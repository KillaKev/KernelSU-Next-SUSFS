#!/usr/bin/env python3
"""
resolve_release.py - turn "a kodiak factory image + which kernel I want" into every value the
build needs, so none of it has to be typed in.

INPUTS
  --factory-url  the Download link of a kodiak image on https://developers.google.com/android/images
  --kernel       match | latest | <dated branch> | <x.y.z> | <release tag>      (default: match)
  --env          file to append KEY=VALUE lines to ($GITHUB_ENV)
  --work         scratch dir (the factory zip is ~3 GB and is deleted as soon as it is read)

WHAT IS READ FROM THE FACTORY IMAGE (the single source of truth for "stock")
  boot.img      -> the kernel's own banner:  uname -r, the build timestamp (uname -v), the KMI
  init_boot.img -> os_version header field:  the device security patch level

WHICH KERNEL IS BUILT
  match   the exact commit named by the -g<hash> in the stock uname. Same source as the factory
          image, so the release string is the stock string by construction.
  other   any other release of the SAME KMI (androidNN-X.Y): a dated branch ("2026-06",
          "android16-6.12-2026-06", "common-android16-6.12-2026-06"), a version ("6.12.81"),
          "latest", or a release tag ("android16-6.12-2026-06_r5"). A branch means its newest
          tagged release. Its uname is NOT guessed: Google publishes a certified GKI boot image for
          every tagged release, and the banner in that image is exactly what a stock kernel of that
          release reports (g<hash> and ab<build> included).

The build timestamp is ALWAYS the factory kernel's, whichever kernel is chosen.
"""
import argparse
import base64
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

AOSP = "https://android.googlesource.com"
COMMON = AOSP + "/kernel/common"
MANIFEST = AOSP + "/kernel/manifest"
GKI_ZIP = "https://dl.google.com/android/gki/gki-certified-boot-{tag}.zip"

RELEASE_RE = re.compile(
    r"^(?P<maj>\d+)\.(?P<min>\d+)\.(?P<sub>\d+)-(?P<android>android\d+)-(?P<gen>\d+)"
    r"-g(?P<hash>[0-9a-f]{7,40})-ab(?P<ab>\d+)(?P<page>-\d+k)?$"
)
BANNER_RE = re.compile(rb"Linux version \d+\.\d+\.\d+[^\n\x00]*")
TS_RE = re.compile(r"(\w{3} \w{3} +\d{1,2} \d\d:\d\d:\d\d \w+ \d{4})\s*$")


def log(msg):
    print(msg, flush=True)


def die(msg):
    print("::error::" + msg, flush=True)
    sys.exit(1)


def http(url, token=None, binary=True, tries=4):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kodiak-ci"})
            if token and "api.github.com" in url:
                req.add_header("Authorization", "Bearer " + token)
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            return data if binary else data.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (404, 403, 401):
                raise
            last = e
        except Exception as e:  # transient network trouble
            last = e
    raise last


def download(url, dest):
    log("downloading " + url)
    subprocess.run(["curl", "-fL", "--retry", "4", "--retry-delay", "5", "-o", dest, url], check=True)
    log("  -> %s (%.1f MB)" % (dest, os.path.getsize(dest) / 1e6))


# ----------------------------------------------------------------------------- remote zip
class RemoteFile(io.RawIOBase):
    """Seekable read-only view of a URL, fetched with HTTP Range requests in 8 MiB blocks."""
    BLOCK = 8 * 1024 * 1024

    def __init__(self, url):
        self.url, self.pos, self.cache = url, 0, {}
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "kodiak-ci"})
        with urllib.request.urlopen(req, timeout=120) as r:
            cr = r.headers.get("Content-Range", "")
            if r.status != 206 or "/" not in cr:
                raise RuntimeError("server does not support range requests")
            self.size = int(cr.rsplit("/", 1)[1])

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = {0: off, 1: self.pos + off, 2: self.size + off}[whence]
        return self.pos

    def _block(self, idx):
        if idx not in self.cache:
            if len(self.cache) > 3:
                self.cache.pop(next(iter(self.cache)))
            lo = idx * self.BLOCK
            hi = min(lo + self.BLOCK, self.size) - 1
            self.cache[idx] = http_range(self.url, lo, hi)
        return self.cache[idx]

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        got = 0
        while got < n:
            idx, off = divmod(self.pos, self.BLOCK)
            chunk = self._block(idx)[off:off + n - got]
            b[got:got + len(chunk)] = chunk
            got += len(chunk)
            self.pos += len(chunk)
        return got


def http_range(url, lo, hi, tries=4):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=%d-%d" % (lo, hi), "User-Agent": "kodiak-ci"})
            with urllib.request.urlopen(req, timeout=300) as r:
                data = r.read()
            if len(data) == hi - lo + 1:
                return data
        except Exception as e:  # transient network trouble
            last = e
    raise RuntimeError("range %d-%d of %s failed: %s" % (lo, hi, url, last))


class SubFile(io.RawIOBase):
    """A [off, off+size) window of another seekable file (a STORED zip member read in place)."""

    def __init__(self, f, off, size):
        self.f, self.off, self.size, self.pos = f, off, size, 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, p, whence=0):
        self.pos = {0: p, 1: self.pos + p, 2: self.size + p}[whence]
        return self.pos

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        self.f.seek(self.off + self.pos)
        d = self.f.read(n)
        b[:len(d)] = d
        self.pos += len(d)
        return len(d)


def open_inner_zip(url, work):
    """The image-kodiak-*.zip inside the factory zip, as a ZipFile.

    The factory zip is ~15 GB, but only a few hundred MB of it is ever needed, and the inner zip is
    STORED (not compressed). So it is read in place over HTTP Range requests. If the server or the
    layout does not allow that, fall back to downloading the whole thing."""
    try:
        outer = zipfile.ZipFile(RemoteFile(url))
        info = next(i for i in outer.infolist() if re.match(r"^.*image-kodiak-.*\.zip$", i.filename))
        if info.compress_type != 0:
            raise RuntimeError("inner zip is compressed")
        rf = outer.fp
        rf.seek(info.header_offset)
        hdr = rf.read(30)
        n, m = struct.unpack("<HH", hdr[26:30])
        inner = zipfile.ZipFile(SubFile(rf, info.header_offset + 30 + n + m, info.file_size))
        log("reading the factory image in place over HTTP ranges (%.1f GB zip, only the needed images are fetched)"
            % (rf.size / 1e9))
        return inner
    except Exception as e:
        log("range reading not possible (%s) - downloading the whole factory zip" % e)
    zpath = os.path.join(work, "factory.zip")
    download(url, zpath)
    with zipfile.ZipFile(zpath) as outer:
        name = next((n for n in outer.namelist() if re.match(r"^.*image-kodiak-.*\.zip$", n)), None)
        if not name:
            die("no image-kodiak-*.zip inside the factory zip: " + ", ".join(outer.namelist()[:20]))
        inner_path = os.path.join(work, "inner.zip")
        with outer.open(name) as src, open(inner_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
    os.remove(zpath)
    return zipfile.ZipFile(inner_path)


# ----------------------------------------------------------------------------- magiskboot
def fetch_magiskboot(dest, token):
    """The magiskboot that ships inside the LATEST Magisk APK (x86_64 build, runs on the runner)."""
    rel = json.loads(http("https://api.github.com/repos/topjohnwu/Magisk/releases/latest", token, binary=False))
    assets = [a for a in rel.get("assets", []) if re.match(r"^Magisk-.*\.apk$", a["name"])]
    if not assets:
        die("no Magisk-*.apk asset in the latest topjohnwu/Magisk release")
    a = assets[0]
    log("Magisk %s: %s" % (rel.get("tag_name"), a["name"]))
    apk = dest + ".apk"
    download(a["browser_download_url"], apk)
    with zipfile.ZipFile(apk) as z:
        with z.open("lib/x86_64/libmagiskboot.so") as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    os.chmod(dest, 0o755)
    os.remove(apk)
    return rel.get("tag_name", "?")


def kernel_from_boot(magiskboot, boot_img, workdir):
    """Unpack a boot image with magiskboot and return the (decompressed) kernel bytes."""
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir)
    shutil.copy(boot_img, os.path.join(workdir, "boot.img"))
    r = subprocess.run([magiskboot, "unpack", "boot.img"], cwd=workdir, capture_output=True, text=True)
    log(r.stdout.strip() + r.stderr.strip())
    k = os.path.join(workdir, "kernel")
    if r.returncode != 0 or not os.path.isfile(k):
        die("magiskboot could not unpack %s (rc=%s)" % (os.path.basename(boot_img), r.returncode))
    with open(k, "rb") as f:
        return f.read()


def parse_banner(kernel):
    m = BANNER_RE.search(kernel)
    if not m:
        die("no 'Linux version' banner in the kernel")
    banner = m.group(0).decode("ascii", "replace").strip()
    release = banner.split()[2]
    t = TS_RE.search(banner)
    if not t:
        die("cannot find the build timestamp at the end of the banner: " + banner)
    return banner, release, t.group(1)


def decode_patch_level(boot_hdr):
    """boot header v3/v4: os_version u32 at offset 16 = (A<<14|B<<7|C)<<11 | (year-2000)<<4 | month."""
    if boot_hdr[:8] != b"ANDROID!":
        return None, None
    (osv,) = struct.unpack_from("<I", boot_hdr, 16)
    lvl = osv & 0x7FF
    if lvl == 0:
        return None, None
    ver = osv >> 11
    return "%d-%02d" % (2000 + (lvl >> 4), lvl & 0xF), "%d.%d.%d" % ((ver >> 14) & 0x7F, (ver >> 7) & 0x7F, ver & 0x7F)


# ----------------------------------------------------------------------------- gitiles / git
def ls_remote(url, *patterns):
    out = subprocess.run(["git", "ls-remote", url, *patterns], check=True, capture_output=True, text=True).stdout
    return [ln.split("\t") for ln in out.splitlines() if "\t" in ln]


def gitiles_commit(ref):
    txt = http("%s/+/%s?format=JSON" % (COMMON, ref), binary=False)
    return json.loads(txt.split("\n", 1)[1])["commit"]


def makefile_version(ref):
    txt = base64.b64decode(http("%s/+/%s/Makefile?format=TEXT" % (COMMON, ref))).decode()
    v = {k: re.search(r"^%s = (\d+)" % k, txt, re.M).group(1) for k in ("VERSION", "PATCHLEVEL", "SUBLEVEL")}
    return "%s.%s.%s" % (v["VERSION"], v["PATCHLEVEL"], v["SUBLEVEL"])


def list_releases(kmi):
    """{tag: commit} for every <kmi>-YYYY-MM_rN tag in kernel/common."""
    tags = {}
    for sha, ref in ls_remote(COMMON, "refs/tags/%s-*_r*" % kmi):
        name = ref[len("refs/tags/"):]
        peeled = name.endswith("^{}")
        name = name[:-3] if peeled else name
        if re.match(r"^%s-\d{4}-\d{2}_r\d+$" % re.escape(kmi), name) and (peeled or name not in tags):
            tags[name] = sha
    return tags


def tag_key(tag):
    m = re.match(r"^.*-(\d{4})-(\d{2})_r(\d+)$", tag)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def branch_of(tag):
    return tag.rsplit("_r", 1)[0]


def manifest_branches(kmi):
    return {ref[len("refs/heads/"):] for _, ref in ls_remote(MANIFEST, "refs/heads/common-%s*" % kmi)}


def gki_release_banner(tag, magiskboot, work):
    """Banner of Google's certified GKI boot image for this tagged release, or None."""
    url = GKI_ZIP.format(tag=tag)
    try:
        data = http(url)
    except urllib.error.HTTPError:
        return None
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if n.endswith(".img")]
        if not names:
            return None
        img = os.path.join(work, "gki-boot.img")
        with open(img, "wb") as f:
            f.write(z.read(sorted(names, key=len)[0]))
    return parse_banner(kernel_from_boot(magiskboot, img, os.path.join(work, "gki-unpacked")))[1]


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--factory-url", required=True)
    ap.add_argument("--kernel", default="match")
    ap.add_argument("--env", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--stock-out", required=True, help="where to keep the factory boot.img")
    ap.add_argument("--magiskboot", required=True, help="where to put magiskboot")
    args = ap.parse_args()
    token = os.environ.get("GH_TOKEN")
    out = {}

    # ---- 1. the factory image -------------------------------------------------------------
    url = args.factory_url.strip()
    m = re.match(r"^https://dl\.google\.com/dl/android/aosp/kodiak-([a-z0-9.]+)-factory-[0-9a-f]+\.zip$", url)
    if not m:
        die("factory_image must be the Download link of a *kodiak* image from "
            "https://developers.google.com/android/images (https://dl.google.com/dl/android/aosp/"
            "kodiak-<build>-factory-<hash>.zip), got: " + url)
    build_id = m.group(1).upper()
    out["FACTORY_BUILD"] = build_id
    log("factory image: kodiak %s" % build_id)

    shutil.rmtree(args.work, ignore_errors=True)
    os.makedirs(args.work)
    mb_ver = fetch_magiskboot(args.magiskboot, token)
    out["MAGISK_VERSION"] = mb_ver

    stock_dir = os.path.dirname(args.stock_out)
    os.makedirs(stock_dir, exist_ok=True)
    inner = open_inner_zip(url, args.work)
    names = set(inner.namelist())
    boot = os.path.join(args.work, "boot.img")
    with inner.open("boot.img") as src, open(boot, "wb") as dst:
        shutil.copyfileobj(src, dst)
    init_hdr = inner.open("init_boot.img").read(4096) if "init_boot.img" in names else b""
    # the partitions the automatic gates (CRC / struct layout) read their stock side from
    for part in ("vendor_kernel_boot", "vendor_boot", "vendor_dlkm", "system_dlkm"):
        if part + ".img" in names:
            with inner.open(part + ".img") as src, open(os.path.join(stock_dir, part + ".img"), "wb") as dst:
                shutil.copyfileobj(src, dst)
            log("kept %s.img (%d bytes)" % (part, os.path.getsize(os.path.join(stock_dir, part + ".img"))))
    inner.close()
    shutil.copy(boot, args.stock_out)
    log("stock boot.img kept at %s (%d bytes)" % (args.stock_out, os.path.getsize(args.stock_out)))

    # ---- 2. what the stock kernel says about itself ---------------------------------------
    kern = kernel_from_boot(args.magiskboot, boot, os.path.join(args.work, "stock-unpacked"))
    with open(os.path.join(stock_dir, "Image"), "wb") as f:
        f.write(kern)
    banner, stock_release, stock_ts = parse_banner(kern)
    log("stock banner : " + banner)
    sm = RELEASE_RE.match(stock_release)
    if not sm:
        die("cannot parse the stock release string: " + stock_release)
    kmi = "%s-%s.%s" % (sm["android"], sm["maj"], sm["min"])
    patch_level, android_ver = decode_patch_level(init_hdr)
    log("stock uname -r  : " + stock_release)
    log("stock uname -v  : #1 SMP PREEMPT " + stock_ts)
    log("KMI             : " + kmi)
    log("patch level     : %s (Android %s, from init_boot.img)" % (patch_level or "unknown", android_ver or "?"))
    out.update(STOCK_RELEASE=stock_release, STOCK_BANNER=banner, STOCK_TS=stock_ts,
               PATCH_LEVEL=patch_level or "unknown", KERNEL_KMI=kmi, SUSFS_BRANCH="gki-" + kmi)

    # ---- 3. which kernel source ------------------------------------------------------------
    sel = args.kernel.strip()
    releases = list_releases(kmi)
    if not releases:
        die("no %s release tags found in kernel/common" % kmi)
    branches = manifest_branches(kmi)
    dated = sorted({branch_of(t) for t in releases}, key=lambda b: tag_key(b + "_r0"))

    tag = None
    if sel in ("", "match"):
        commit = gitiles_commit(sm["hash"])
        sub = makefile_version(commit)
        if sub != "%s.%s.%s" % (sm["maj"], sm["min"], sm["sub"]):
            die("commit %s is %s, but the stock uname says %s.%s.%s" % (commit[:12], sub, sm["maj"], sm["min"], sm["sub"]))
        tags_here = [t for t, c in releases.items() if c == commit]
        tag = tags_here[0] if tags_here else None
        # manifest branch: the dated branch whose head carries the same sublevel
        branch = None
        for b in dated:
            try:
                if makefile_version("refs/heads/" + b) == sub:
                    branch = b
                    break
            except urllib.error.HTTPError:
                continue  # old release with no branch head any more
        if not branch:
            die("no dated %s branch is at %s any more - cannot pick a manifest for commit %s" % (kmi, sub, commit[:12]))
        target_release = stock_release
        out["KERNEL_MODE"] = "match (exact commit %s%s)" % (commit[:12], " = tag " + tag if tag else "")
    else:
        s = sel
        if s == "latest":
            tag = max(releases, key=tag_key)
        elif re.match(r"^(?:.*-)?\d{4}-\d{2}_r\d+$", s):
            s = s[len("common-"):] if s.startswith("common-") else s
            tag = s if s.startswith(kmi) else "%s-%s" % (kmi, s)
        elif re.match(r"^\d+\.\d+\.\d+$", s):
            want = s
            tag = None
            for b in reversed(dated):
                for t in sorted((t for t in releases if branch_of(t) == b), key=tag_key, reverse=True):
                    if makefile_version(releases[t]) == want:
                        tag = t
                        break
                if tag:
                    break
            if not tag:
                die("no %s release carries kernel %s" % (kmi, want))
        else:
            s = s[len("common-"):] if s.startswith("common-") else s
            b = s if s.startswith(kmi) else "%s-%s" % (kmi, s)
            cands = [t for t in releases if branch_of(t) == b]
            if not cands:
                die("no tagged releases for branch %s (known: %s)" % (b, ", ".join(dated)))
            tag = max(cands, key=tag_key)
        if tag not in releases:
            die("release tag %s does not exist; newest are: %s" % (tag, ", ".join(sorted(releases, key=tag_key)[-5:])))
        commit = releases[tag]
        branch = branch_of(tag)
        sub = makefile_version(commit)
        log("selected release: %s (%s, commit %s)" % (tag, sub, commit[:12]))
        target_release = gki_release_banner(tag, args.magiskboot, args.work)
        if target_release:
            log("uname -r of Google's certified GKI build of %s: %s" % (tag, target_release))
        else:
            h = commit[:12]
            target_release = "%s-%s-%s-g%s-ab%s%s" % (sub, sm["android"], sm["gen"], h, sm["ab"], sm["page"] or "")
            print("::warning::no certified GKI boot image published for %s - uname composed as %s "
                  "(KMI generation and ab number borrowed from the factory kernel)" % (tag, target_release), flush=True)
        out["KERNEL_MODE"] = "other release %s" % tag

    tm = RELEASE_RE.match(target_release)
    if not tm or "%s-%s.%s" % (tm["android"], tm["maj"], tm["min"]) != kmi:
        die("target release %s is not on the factory kernel's KMI (%s)" % (target_release, kmi))
    if "common-" + branch not in branches:
        die("kernel/manifest has no branch common-%s (have: %s)" % (branch, ", ".join(sorted(branches))))

    out.update(TARGET_RELEASE=target_release, KERNEL_COMMIT=commit, KERNEL_MANIFEST_BRANCH="common-" + branch,
               KERNEL_SUBLEVEL=sub.split(".")[2], KERNEL_VERSION=sub, KERNEL_TAG=tag or "",
               KERNEL_PREFIX="%s-%s-" % (sub, tm["android"]), BUILD_NUMBER=tm["ab"])
    out["FILE_NAME"] = "kodiak-%s-%s" % (build_id.lower(), target_release.rsplit("-ab", 1)[0].split("-android")[0])
    log("")
    log("=== resolved ===")
    for k, v in out.items():
        log("%-22s %s" % (k, v))
    with open(args.env, "a") as f:
        for k, v in out.items():
            f.write("%s=%s\n" % (k, v))


if __name__ == "__main__":
    main()
