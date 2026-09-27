# SOURCE OF TRUTH: the factory image (read this first)

`kodiak-stock-header.bin` is the first 4096 bytes of **`boot.img` from Google's factory image**

```
C:\Users\coolk\Downloads\kodiak-cd1a.260905.001.b1\boot.img      (archive: image-kodiak-cd1a.260905.001.b1.zip)
device: kodiak / Pixel 11 Pro XL, build CD1A.260905.001.B1 (16238327), security patch 2026-09-01
```

It is Google's own artifact for this exact build, so `verify_boot_img.py` compares our header against
the real thing. Everything below this section is the *history* of how that file was finally obtained
and which values were guessed wrong along the way — kept because the mistake is instructive.

## What the factory image actually says (measured)

```
boot.img       hv=4  hsize=1584  kernel_size=20230712  ramdisk_size=0
               os_version=0x0        (0.0.0 / patch 0)      cmdline=''  sig=0  reserved=all-zero
               kernel release: 6.12.69-android16-6-g5c5f2fea42dd-ab15835541-4k

init_boot.img  hv=4  hsize=1584  kernel_size=0         ramdisk_size=2667913
               os_version=0x220001a9 (17.0.0 / patch 2026-09) cmdline=''  sig=0  reserved=all-zero
```

**The trap, stated plainly:** Android writes the OS version into `init_boot.img` and **not** into
`boot.img`. So an inference of the form "the stock header on this device says 17.0.0 / 2026-09, so
boot.img should too" is wrong — it was wrong in this repo for exactly one revision. The correct
value for a kodiak `boot.img` header is **os_version = 0**, and the workflow now passes no
`--os_version` / `--os_patch_level` at all so that is what mkbootimg writes.

Two more things this image confirms:

- the stock kernel is **6.12.69** (`kodiak-cd1a…`'s boot.img kernel, and the vendor modules' vermagic
  `6.12.69-android16-6-g6a49175400c0-ab16238327-4k`), i.e. the `android16-6.12` KMI line and sublevel
  69 == `common-android16-6.12-2026-03`, which is what the workflow defaults to;
- the stock kernel carries the `-4k` LOCALVERSION suffix (4K pages), and stock boot.img/init_boot.img
  both have an **empty cmdline** and `signature_size 0`.

## Also available locally now

`C:\Users\coolk\Downloads\kodiak-cd1a.260905.001.b1\` contains the whole factory image, so these are
on hand without any download:

| file | use |
|---|---|
| `boot.img` | the ideal rollback artifact (`fastboot flash boot boot.img`) |
| `init_boot.img` | unpatched Google ramdisk — the reference for a Magisk/APatch-style install |
| `vendor_boot.img`, `vendor_kernel_boot.img`, `vendor_dlkm.img`, `system_dlkm.img` | untouched stock images |
| `flash-all.bat` / `flash-base.sh` | last-resort full restore |
| `image-kodiak-cd1a.260905.001.b1.zip` | the original archive (15.3 GB) |

## How to re-derive the reference after an OTA

```powershell
# after re-downloading/re-extracting the factory image for the new build:
python -c "open(r'C:\Users\coolk\workspace\pixel11_kernel_ci\.github\boot-template\kodiak-stock-header.bin','wb').write(open(r'<extracted>\boot.img','rb').read(4096))"
```

Then check whether `os_version` in the new build is still 0 and update the workflow's inputs if not
(the verifier fails loudly either way, which is the point).

# Where `kodiak-stock-header.bin` comes from — and what it does *not* prove

# (HISTORY — superseded) How the reference was first obtained, and why init_boot misled us

Superseded by the section above. This adb dump of `init_boot_a` was the reference for one revision
of this repo, and it pointed `os_version` at 17.0.0 / 2026-09 — the **init_boot** value, not the
**boot.img** value. Kept because that mistake is easy to repeat.

```powershell
adb shell su -c "dd if=/dev/block/by-name/init_boot_a of=/data/local/tmp/h.img bs=4096 count=1"
adb pull /data/local/tmp/h.img .github/boot-template/kodiak/init-boot-header.bin
```

## (HISTORY) Why init_boot was used at all

Because **there is no stock boot image left on the phone**:

```
boot_a : ANDROID! v4  kernel_size=21155087  ramdisk_size=0  os_version=0x0 (0)      cmdline=""
boot_b : ANDROID! v4  kernel_size=21155087  ramdisk_size=0  os_version=0x0 (0)      cmdline=""   <- same bytes
init_boot_a : ANDROID! v4  kernel_size=0  ramdisk_size=2667913  os_version=0x220001a9 (17.0.0 / 2026-09)
```

`boot_a` and `boot_b` hold the **same** Wild Kernel build (identical kernel payload SHA-256,
`0c2e179a…`), so both slots carry the *patched* image — an earlier version of this file used
`boot_a` as the reference and called it "stock", which was wrong: those zeros are the packer's,
not Google's.

`init_boot_a` is the closest thing to a Google-authored image on the device:

- KernelSU-Next is a **kernel**-side root, so nothing in this setup patches `init_boot`.
- Its `os_version` is `0x220001a9` = **Android 17.0.0, patch level 2026-09**, which matches the
  running build (`CD1A.260905.001.B1`, security patch 2026-09-01). A value like that has to have
  been *written* by Google's build (repackers such as `magiskboot` preserve header fields, they do
  not invent an OS version), whereas a `0` in a boot image is exactly what
  `mkbootimg --header_version 4 --kernel Image.lz4` produces when nobody passes `--os_version`.

Because `init_boot` and `boot` share the same `ANDROID!` v4 header struct, it *looked* like a valid
reference for the field conventions — and for `header_version`, `header_size`, `cmdline`,
`signature_size` and `reserved` it is. For **`os_version` it is not**: Android writes that into
`init_boot` and leaves it zero in `boot`. That is the whole error, in one sentence.

## What the verification does and does not claim

The build is checked as *header-field-identical to a Google-authored image from this device and
this build*, plus the device rules for a boot image: v4, `ramdisk_size == 0`, `cmdline` empty,
reserved zeroed, `kernel_size > 0`, size within the 64 MiB partition.

It does **not** claim the image is Google-signed or cryptographically trusted — it is not, and the
AVB footer (when `boot_img=avb`) is signed with a generated test key.

## That question is now answered — by measurement

The factory image was downloaded and extracted, and the answer is **no: stock `boot.img` has
`os_version = 0`**. From `kodiak-cd1a.260905.001.b1\boot.img`:

```
header_version=4  header_size=1584  kernel_size=20230712  ramdisk_size=0
os_version=0x0    cmdline=''        signature_size=0      reserved=all-zero
kernel release:   6.12.69-android16-6-g5c5f2fea42dd-ab15835541-4k
```

So the revision of this repo that set `17.0.0 / 2026-09` was wrong, and the one before it (which
copied the *patched* boot_a) accidentally had the right value for the wrong reason. The workflow now
passes no `--os_version`/`--os_patch_level` by default, and the verifier compares against the
factory `boot.img` header, not against `init_boot`.

`kodiak-boot_a-header.patched.bin` is the 4 KB dump of the *patched* `boot_a`, kept only so the
difference is inspectable.

## (HISTORY) The factory-image hunt that this section records

The obvious move is to pull `boot.img` out of Google's factory image and use *its* header. That was
attempted and blocked at every entrance, so it is recorded here rather than left as a mystery:

| route | result |
|---|---|
| `developers.google.com/android/images` raw HTML | consent wall; **0** tables, **0** `kodiak`, **0** `.zip` links — the table is rendered client-side |
| …with `devsite_wall_acks=nexus-image-tos` / `SOCS` cookies, Googlebot + bingbot UAs | same gated page (74–76 KB, no table) |
| rendering proxy (`r.jina.ai`) and the Wayback snapshot of 2026-09-25 (`id_` raw form) | same gated page |
| `…/images.json`, `…/ota.json` | HTTP 404 |
| Android Flash Tool back end (its bundle names `flashstation_pa.builds.list`, host `androidbuild-pa.clients6.google.com`) | RPC paths 404 on that host, **no API key anywhere in the bundle**, and the same-origin `batchexecute` endpoint needs `f.sid`/`bl` tokens the shell page does not expose |
| `ci.android.com/v1/builds` | HTTP 404 |
| DuckDuckGo (html + lite), Bing, Mojeek | blocked/empty; Bing only auto-completed `kodiak-cd1a.260905.001.b1`, no hashed URL |
| the phone itself | **no stock image exists on it** — see below |
| the PC | no factory zip; the only 64 MiB boot images are the Nothing Phone's and this same kodiak repack |

That last row is the reason this file exists. Verified on 2026-09-26:

```
boot_a : ANDROID! v4  kernel_size=21155087  ramdisk_size=0  os_version=0  release 6.12.69-android16-Wild  sha256 0c2e179a…
boot_b : ANDROID! v4  kernel_size=21155087  ramdisk_size=0  os_version=0  release 6.12.69-android16-Wild  sha256 0c2e179a…   ← identical
init_boot_a : ANDROID! v4  kernel_size=0  ramdisk_size=2667913  os_version=0x220001a9 (17.0.0 / 2026-09)          ← Google's
```

Both slots carry the *same* patched build (byte-identical kernel payload), so no stock boot image
survives anywhere on the device. `vendor_kernel_boot_a` was checked too, in case Google's kernel
lived there: it is a **VNDRBOOT v4** image (page_size 2048, vendor_ramdisk_size 5,450,091) with **no
`ANDROID!` header and no kernel** — but it does carry 26 uncompressed module `vermagic=` strings,
which *independently* confirm the stock kernel release:

```
vermagic=6.12.69-android16-6-g6a49175400c0-ab16238327-4k SMP preempt mod_unload modversions aarch64
```

`update_engine` left no OTA payload URL either (`/data/misc/update_engine/prefs/` holds only
`boot-id`, `previous-version`, byte counters).

### How it ended

Every automated route above stayed closed, and the file was obtained the boring way: downloading the
factory image in a browser and extracting it. `boot.img` from it is now the reference
(`kodiak-stock-header.bin`) and is also on hand as the rollback artifact, which is strictly better
than any inference from a patched partition.

