# GKI + KernelSU-Next + SUSFS kernel for Pixel 11 Pro XL (kodiak)

Builds a **GKI kernel with KernelSU-Next and SUSFS** for:

| | |
|---|---|
| Device | Pixel 11 Pro XL / **kodiak** / Tensor G6 |
| Build | Android **17**, `CD1A.260905.001.B1` (16238327) |
| Kernel now on the phone | `6.12.69-android16-9-g84174099` (Wild Kernel) |
| Stock kernel lineage | `6.12.69-android16-6-g6a49175400c0-ab16238327-4k` |
| KMI | **android16-6.12** (GKI) |
| Output | AnyKernel3 zip + raw `Image` / `Image.lz4` |

This is the same shape as the Nothing Phone (2a) `KernelSU-Next-SUSFS` repo, but every
Pixel-specific decision below is pinned to evidence read off the device rather than to
habit. The one that matters is the KMI line.

---

## Why `android16-6.12`, and not `android17-6.18`

Android 17's ACK line is **6.18** (`common-android17-6.18-*`, confirmed from
`kernel/manifest`), and it would be easy to reach for it. It would also produce a kernel
whose vendor modules cannot load.

```
$ adb shell uname -r
6.12.69-android16-9-g84174099

$ adb shell su -c "strings /vendor/lib/modules/*.ko | grep -m1 vermagic="
vermagic=6.12.69-android16-6-g6a49175400c0-ab16238327-4k SMP preempt mod_unload modversions aarch64

$ adb shell su -c "ls /vendor/lib/modules | wc -l"
128
```

The device is **`CONFIG_MODVERSIONS=y`** (`modversions` in that vermagic), so the kernel
compares **symbol CRCs** against the 128 prebuilt modules Google ships in
`/vendor/lib/modules`. Those CRCs are frozen per **KMI generation** — which is exactly what
Google's branch names encode. Building from `android17-6.18` gives a different CRC set and
every vendor module rejects with `disagrees about version of symbol`: dead Wi-Fi, camera,
audio, or no boot at all.

Within the KMI line the dated branches are interchangeable for CRC purposes:

```
common-android16-6.12-2026-03   v6.12.69   <<< the sublevel the phone is running now
common-android16-6.12-2026-06   v6.12.81
common-android16-6.12-2026-09   newest dated release of this KMI line
common-android16-6.12-lts       moving target
```

The default here is **2026-03 / 6.12.69**, because that reproduces the sublevel the device
boots today — the least surprising thing to hand a Pixel. The workflow gates on the
`Makefile` `SUBLEVEL` matching `expected_sublevel`, so a wrong branch fails in two minutes
instead of after a two-hour build plus a flash.

Two further facts that make this work at all:

- **`# CONFIG_MODULE_SIG_FORCE is not set`** on the device → unsigned modules may load,
  which is what lets KernelSU/SUSFS work in the first place.
- With `CONFIG_MODVERSIONS=y` the kernel's `same_magic(..., has_crcs=true)` **skips the
  version token** of the vermagic and compares only the flag part, so the CRCs are the real
  gate, not the release string. That is why a kernel reading `6.12.69-android16-9-g84174099`
  happily loads modules built for `6.12.69-android16-6-…-ab16238327-4k`.

*(`CONFIG_MODULE_SCMVERSION` is therefore **not** pinned here — unlike the phone-2a build,
where the stock vendor modules carried it. This device's vermagic has no `scmversion`
token, so there is nothing to match.)*

---

## Where this lives

It is published into the repo that already hosts the Nothing Phone (pacman) pipeline —
`KillaKev/KernelSU-Next-SUSFS` — as a **second workflow**, on a branch so the change is reviewable:

| in the repo | what it is |
|---|---|
| `.github/workflows/build-kernel-kodiak.yml` | this workflow (pacman keeps `build-kernel.yml`) |
| `.github/scripts/kodiak/susfs.config` | the kconfig fragment (pacman keeps `.github/scripts/susfs.config`) |
| `.github/scripts/kodiak/verify_boot_img.py`, `verify_kernel_image.sh` | the gates |
| `.github/boot-template/kodiak/{stock-header.bin,init-boot-header.bin,boot_a-header.patched.bin,SOURCE.md}` | reference headers + provenance |
| `README-kodiak.md` | this document (the repo's root README is the pacman one) |

The `kodiak/` namespace is not cosmetic: the repo already contains `verify_boot_img.py`,
`susfs.config`, a `boot-template/` directory, a root `README.md` and `.gitattributes` for the Nothing
build, so writing to those paths would have silently broken a working pipeline.

## Publishing it

```powershell
# plan only: clones, checks for collisions, stages the additions, pushes nothing
powershell -ExecutionPolicy Bypass -File C:\Users\coolk\workspace\pixel11_kernel_ci\push_update.ps1

# create the branch and publish
powershell -ExecutionPolicy Bypass -File C:\Users\coolk\workspace\pixel11_kernel_ci\push_update.ps1 -Push
```

The script only ever **adds** files: it aborts if a destination already exists with different
content, and it aborts if the staged change is anything other than new files (status `A`). It pushes
to a branch (`kodiak-ci` by default), never to `main` — merge when you are happy, or delete the branch
to undo.

## Running the build

**Actions → Build GKI kernel (KernelSU-Next + SUSFS) - Pixel 11 Pro XL / kodiak → Run workflow**,
or from this folder:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\coolk\workspace\pixel11_kernel_ci\trigger_run.ps1 -Watch
```

| Input | Default | Meaning |
|---|---|---|
| `kernel_branch` | `common-android16-6.12-2026-03` | ACK branch in `kernel/manifest`. Must be a `common-android16-6.12-*` branch. |
| `expected_sublevel` | `69` | Gate: the synced tree's `Makefile` `SUBLEVEL` must equal this. `69` = 2026-03 = the sublevel the phone runs. Empty string skips the gate. |
| `ksu_repo` | `https://github.com/pershoot/KernelSU-Next.git` | KernelSU-Next fork that carries SUSFS. |
| `ksu_branch` | `dev-susfs` | The SUSFS-carrying branch. Upstream `dev` has no SUSFS. |
| `susfs_branch` | `gki-android16-6.12` | susfs4ksu branch on **GitLab**. Its `kernel_patches/50_add_susfs_in_gki-android16-6.12.patch` must exist (gated). |
| `susfs_commit` | `75a61385…1a2e` | Pin, so the SUSFS side cannot drift under you. Empty = branch tip; the default is the commit the WildKernels 6.12 builds use. |
| `os_patch_level` | `2026-09` | Device security patch level. Pins the kernel build clock and is recorded in the summary. |
| `patches` | `ksu-susfs` | `ksu-susfs`, `ksun-only` (no SUSFS), or `none` = **VANILLA CONTROL**. |
| `bypass_module_versions` | `false` | Also build a second `Image` with `kernel/module/version.c` `bad_version:` neutered. Last resort only. |
| `boot_img` | `plain` | Also produce a **flashable `boot.img`** for `fastboot flash boot`. `none` = AnyKernel3 only, `plain` = mkbootimg header v4 (which *is* the stock layout), `avb` = same plus an AVB hash footer. |
| `avb_partition_size` | `67108864` | boot partition size in bytes, used only for `boot_img=avb`. Measured on the device: boot_a and boot_b are exactly 64 MiB. |
| `bootimg_os_version` | *(empty)* | Optional `mkbootimg --os_version`. **Leave empty.** Google's factory `boot.img` for this build has `os_version = 0`; only `init_boot.img` carries 17.0.0. |
| `bootimg_os_patch_level` | *(empty)* | Optional `mkbootimg --os_patch_level`. Leave empty for the same reason (stock boot.img patch level is 0, stock init_boot's is 2026-09). |

Expect **~45–90 minutes** for `ksu-susfs` (kleaf `--config=fast --config=stamp`, one
architecture, no GKI module build).

While it runs:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\coolk\workspace\pixel11_kernel_ci\fetch_log.ps1 -StatusOnly
powershell -ExecutionPolicy Bypass -File C:\Users\coolk\workspace\pixel11_kernel_ci\fetch_log.ps1
```

### `patches: none` is not a toy

The vanilla control builds the *same* pinned tree, the *same* patched-`gki_defconfig` path and
the *same* release-string machinery, but with no KernelSU/SUSFS at all. If a build fails, that
separates "the recipe/kleaf is wrong" from "the patches are wrong" in one run — the same trick
that paid for itself on the phone-2a pipeline.

---

## What each step does

| # | Step | Why it exists |
|---|---|---|
| 1 | Free disk space | The runner died with `No space left on device` writing its own log on a previous project. Reclaims, then fails loudly if <45 GB is free. |
| 2 | Add swap | Plain 16 GB swapfile (no third-party action). clang/lld spikes past the runner's RAM; an OOM kill mid-compile looks like a mystery failure. |
| 3 | `repo init` + `repo sync` | kleaf needs the whole multi-repo tree (`common`, `build`, `prebuilts`). Asserts the branch exists first and prints the candidates if not. 3 attempts, since the sync pulls tens of GB. |
| 4 | Sublevel / KMI gate | The cheapest guard there is: `6.12` line + `SUBLEVEL == expected`. Wrong branch = wrong release string; wrong KMI line = modules do not load. |
| 5 | Pin the build clock | `SOURCE_DATE_EPOCH` / `KBUILD_BUILD_TIMESTAMP` derived from the device's patch level, so rebuilds are reproducible. |
| 6 | KernelSU-Next install | Its own `kernel/setup.sh` does the tree wiring. **Never** apply susfs4ksu's `10_enable_susfs_for_ksu.patch` on top - it targets upstream KernelSU and only produces rejects. |
| 7 | KSU↔SUSFS API gate | Fails in ~2 min if the fork's `Kconfig` lacks `KSU_SUSFS`; otherwise every `CONFIG_KSU_SUSFS*` would be silently dropped and SUSFS would hide nothing. |
| 8 | SUSFS patch | GitLab clone, optional commit pin, the two **6.12 pre-patch fixups** (`>=58` drop the `dma-buf` include in `fs/exec.c`; `>=69` `vma_data_pages` → `vma_pages`) and the two **post-patch helpers** (`VMA_PAD_START`, `page_size_compat.h`). Any `.rej` fails the run. |
| 9 | Config | `.github/scripts/kodiak/susfs.config` patched into `gki_defconfig`, then re-grepped; the control build asserts `CONFIG_KSU*` is absent. |
| 10 | Disable `check_defconfig` | kleaf's own guard would fail the build for patching `gki_defconfig` - which is the point for us. Also drops the ABI protected-exports list, since KSU/SUSFS export extra symbols. |
| 11 | Kill `-dirty`/`-maybe-dirty` | Otherwise `UTS_RELEASE` ends in `-maybe-dirty`, and that string reaches the vermagic and the device. Also commits the tree so SCM sees it clean; then asserts. |
| 12 | kleaf build | `--config=fast --config=stamp //common:kernel_aarch64/Image //common:kernel_aarch64/Image.lz4`. Only the kernel Image: the 128 vendor modules are prebuilt on-device. |
| 13 | (optional) bypass Image | Only when the input asks. |
| 14 | Verify the Image | `.github/scripts/kodiak/verify_kernel_image.sh`: arm64 magic at `0x38`, release prefix, no dirty marker, and KernelSU/SUSFS markers present - or **absent** for the control. |
| 15 | boot.img (optional) | `mkbootimg.py --header_version 4 --kernel Image.lz4` (no `--os_version`, exactly like stock) from AOSP's `platform/system/tools/mkbootimg`, then `verify_boot_img.py` checks every header field against `.github/boot-template/kodiak/stock-header.bin` — the first 4 KB of **Google's factory `boot.img`**. `avb` mode adds an `avbtool add_hash_footer` and checks the size against the real partition. |
| 16 | AnyKernel3 | Clones the GKI AnyKernel3, forces `do.devicecheck=1` + `device.name1=kodiak` (upstream ships it OFF, i.e. flashable to any GKI device), drops in `Image`/`Image.lz4`, zips, uploads. |
| 17 | Summary | A run summary with the refs, the artifact name and both flashing routes. |

---

## Flashing — and the one thing to do first

**Back up the boot partition you are about to replace.** The phone is rooted, so this is one
command and it is the whole rollback story:

```powershell
$PT="C:\Users\coolk\Downloads\platform-tools"
& "$PT\adb.exe" shell su -c "dd if=/dev/block/by-name/boot_a of=/sdcard/boot_a-pre-ci.img"
& "$PT\adb.exe" pull /sdcard/boot_a-pre-ci.img C:\Users\coolk\workspace\pixel11\boot_a-pre-ci.img
```

*(The phone is on slot `_a`; on slot `_b` both the backup source and the flash target are
`boot_b`. The Wild Kernel zip you already have is a second, independent rollback path.)*

### Path 1 — AnyKernel3 (recommended)

Install `<artifact>-AnyKernel3.zip` with **KernelFlasher** (or any AnyKernel3 flasher).

Why this is the default: AK3 repacks the **live** boot partition **on the phone** with
`magiskboot`, so the boot header (kernel/ramdisk addresses, OS version, patch level), the
ramdisk and the AVB metadata are preserved byte-for-byte, and `patch_vbmeta_flag=auto` handles
the verification flags. It refuses a non-6.12 kernel outright, and this repo's zip has
`do.devicecheck=1` + `device.name1=kodiak`, so it will not flash to any other device.

### Path 2 — Image.lz4 + MagiskBoot (no flasher app)

```powershell
& "$PT\adb.exe" push <artifact>-Image.lz4 /data/local/tmp/
& "$PT\adb.exe" shell su -c "cd /data/local/tmp && magiskboot unpack /dev/block/by-name/boot_a && cp Image.lz4 kernel && magiskboot repack /dev/block/by-name/boot_a new-boot.img && dd if=new-boot.img of=/dev/block/by-name/boot_a"
```

MagiskBoot can be taken from any AnyKernel3 zip or the Magisk APK's `lib/arm64-v8a/`.

### Path 3 — `boot.img` through fastboot (`boot_img: plain` or `avb`)

```powershell
& "$PT\fastboot.exe" flash boot C:\path\to\<artifact>-boot.img
& "$PT\fastboot.exe" reboot
```

This is not guesswork — it is now *measured* against Google's own factory image for this build.

The header is **v4**, which carries no kernel/ramdisk/tags addresses and a fixed 4096-byte page
size — so the image is a 1584-byte header + padding to 4096 + the kernel. The fields that could
still be wrong are `os_version`, `os_patch_level`, `cmdline` and `signature_size`, and the trap
turned out to be `os_version`, because **Android writes that into `init_boot.img` and leaves it zero
in `boot.img`**:

```
                          header  kernel_size  ramdisk_size  os_version             cmdline
boot.img       (factory)  v4          20230712             0  0x0          0.0.0     ""
init_boot.img  (factory)  v4                 0       2667913  0x220001a9   17.0.0/09  ""
```

So stock kodiak `boot.img` has **`os_version = 0`** (patch level 0 too), while `init_boot.img` carries
17.0.0 / 2026-09. Two earlier revisions of this file got that wrong in opposite directions: the first
copied the *patched* `boot_a` (right value, wrong provenance), the second inferred from `init_boot`
(wrong value). That is precisely why the reference is now the factory image's `boot.img`.

`.github/boot-template/kodiak/stock-header.bin` is the first 4 KB of that file, so
`verify_boot_img.py` compares our header field-for-field against Google's. The build passes **no**
`--os_version` / `--os_patch_level` by default (mkbootimg then writes 0, like stock); the two inputs
exist only so a future OTA that changes this can be accommodated, and the verifier fails the run if the
result stops matching. Full provenance — including the failed attempts to obtain the image
programmatically — is in [`.github/boot-template/kodiak/SOURCE.md`](.github/boot-template/kodiak/SOURCE.md).

Use this route when the phone is **not booted** (a bad kernel → bootloop → fastboot is the only
way back in), or when you would rather not depend on a flasher app. On a normal, booted phone
Path 1 is still lower risk: it replaces only the kernel inside the live boot image, whereas this
replaces the whole boot partition.

`avb` mode additionally appends an `avbtool add_hash_footer` with a freshly generated test key,
sized to the real 64 MiB partition — what WildKernels ship. `plain` leaves `signature_size 0`,
exactly like stock; on an unlocked bootloader both flash and boot.

To refresh the reference after an OTA (re-extract the factory image, then):

```powershell
python -c "open(r'C:\Users\coolk\workspace\pixel11_kernel_ci\.github\boot-template\kodiak-stock-header.bin','wb').write(open(r'C:\Users\coolk\Downloads\<new-build>\boot.img','rb').read(4096))"
```

The other dumps in that folder are kept for the record: `kodiak-init_boot-header.bin` (the init_boot
header whose `os_version` misled an earlier revision) and `kodiak-boot_a-header.patched.bin` (the
patched slot-A header).

A note on what this purchase gives you: the footer is signed with a **generated test key**, so
the image is not "trusted" by Google in any cryptographic sense — that is not what AVB is for
here. It exists so that the partition has a well-formed footer of the right size, which is what
the flash path expects. Your device's `green`/`locked` state comes from your own boot-chain
setup, not from this signature.

---

## Verify AFTER flashing — this is the step that matters

```powershell
& "$PT\adb.exe" shell uname -r                                   # expect 6.12.69-android16-... (NOT -maybe-dirty)
& "$PT\adb.exe" shell su -c id                                   # uid=0(root)
& "$PT\adb.exe" shell getprop ro.boot.verifiedbootstate          # green
& "$PT\adb.exe" shell getprop ro.boot.flash.locked               # 1
& "$PT\adb.exe" shell getprop ro.boot.vbmeta.device_state        # locked

# THE critical one - the 128 vendor modules must load
& "$PT\adb.exe" shell su -c "dmesg | grep -iE 'disagrees about version|module verification failed|Unknown symbol'"
```

| Check | Expected |
|---|---|
| `uname -r` | `6.12.69-android16-…` — same lineage, no `dirty` marker |
| `su -c id` | `uid=0(root)` |
| `verifiedbootstate` / `flash.locked` / `device_state` | `green` / `1` / `locked` — untouched by `boot`, so it must persist |
| **the dmesg grep** | **no output.** Any hit = KMI/CRC mismatch → roll back |
| Wi-Fi / camera / audio / cellular | all still working (they *are* vendor modules) |
| KernelSU-Next manager | opens, shows its version, **SuSFS Controls & Info** present |

Then re-run whatever integrity checks matter to you (Play Integrity API Checker, Wallet if you
use it). A custom kernel changes the *kernel*; the hiding stack (SUSFS + your TrickyStore/keybox
setup) is what keeps PI happy — and the SUSFS options here are the same set the Wild Kernel you
run today uses, so that balance should not change.

---

## Rollback (20 seconds, no data loss)

Best option now that the factory image is on this PC — a **Google-signed stock image**, no backup
needed:

```powershell
& "$PT\fastboot.exe" flash boot C:\Users\coolk\Downloads\kodiak-cd1a.260905.001.b1\boot.img
& "$PT\fastboot.exe" reboot
```

That flashes the active slot (`boot`); add `boot_b` too if you want both slots stock. Alternatively
flash your own pre-change dump (`boot_a-pre-ci.img`) or reflash the Wild Kernel zip you were running.
With a Google-signed `boot.img` sitting on disk there is never a reason to tolerate a misbehaving
kernel.

---

## Things worth knowing

- **`/vendor/lib/modules` must keep loading.** That is the single most likely failure mode and
  the reason the ACK branch is pinned to the KMI line so precisely.
- **LTO mode does not affect CRCs or vermagic**, so `--config=fast` (no full LTO) is safe and
  much faster than a `--config=release` build.
- **`--config=stamp` is deliberate**: it is what makes kleaf derive `UTS_RELEASE` from SCM the
  way Google does (and what keeps `LOCALVERSION_AUTO`/`MODULE_SCMVERSION` selectable), with the
  `-maybe-dirty` suffix stripped in step 11.
- **The build produces no GKI modules** (`//common:kernel_aarch64/Image`, not `…_dist`). The
  device's modules are prebuilt and load by KMI; `_dist` would add tens of GB and hours for
  nothing.
- **This kernel is leaner than the Wild Kernel on the phone today.** WildKernels' 6.12 builds
  also add BBG (Baseband-guard), ipset/netfilter options, BBRv3, ntsync and assorted patches
  from `github.com/WildKernels/kernel_patches`. None of that is KernelSU/SUSFS, so it is not
  here — if you want it, port the relevant `Wild_Kernel_Builder/.github/actions/*` step into
  this workflow (each one is a small, self-contained patch + config list).
- **`CONFIG_KSU=y` is required** — it is not `default y` in this fork, and a kernel without it
  has no root at all.
- **The bypass kernel is a real trade**: it accepts modules whose CRCs do not match, which is
  what you want *only* if an official module refuses to load. It weakens exactly the check that
  protects you from running a mismatch, so keep it out of the normal chain.
- **A run is not proof until the phone is booted.** The verification table above is the actual
  acceptance test; the CI gates only make sure a *wrong* artifact is never produced.

## Where the recipe comes from

Everything Pixel-specific here was verified against working, published 6.12 GKI pipelines
rather than guessed:

- `github.com/WildKernels/Wild_Kernel_Builder` — the per-KMI-line GKI builder.
  `.github/config/android16-6.12.json` lists sublevel 69 = `2026-03`, which is exactly the
  sublevel this phone runs; it also provided the SUSFS config list, the two 6.12 SUSFS patch
  fixups, the commit pins (`commits.json`) and the `--config=fast --config=stamp` kleaf
  invocation.
- `github.com/WildKernels/AnyKernel3` (`gki-2.0`) — the packaging, its `block=boot`,
  `patch_vbmeta_flag=auto` and its 6.12 GKI check.
- `gitlab.com/simonpunk/susfs4ksu` — SUSFS itself, branch `gki-android16-6.12`.
- `github.com/pershoot/KernelSU-Next` — the KernelSU fork that carries SUSFS.
- Google's `kernel/manifest` refs — for the branch naming (`common-android16-6.12-*`,
  `common-android17-6.18-*`), so the KMI-line claim is checkable rather than folklore.

And the device itself, for the two facts the whole thing hangs on:

```
$ adb shell uname -r
6.12.69-android16-9-g84174099
$ adb shell su -c "strings /vendor/lib/modules/*.ko | grep -m1 vermagic="
vermagic=6.12.69-android16-6-g6a49175400c0-ab16238327-4k SMP preempt mod_unload modversions aarch64
```
