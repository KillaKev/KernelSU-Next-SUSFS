# GKI + KernelSU-Next + SUSFS kernel for Nothing Phone 2a (pacman)

Builds a **GKI kernel with KernelSU-Next and SUSFS** for:

| | |
|---|---|
| Device | Nothing Phone (2a) / **pacman** / A142 |
| Build | NothingOS 4, `2608130941` |
| Kernel | `5.15.197-android13-8-00005-g2d8ad9139b89-ab15063902` |
| KMI | `android13-5.15` (GKI) |

This exists because the machine that was going to build it locally lost its WSL install, and
because the user already had a working GitHub Actions + kleaf pipeline.

---

## Why `android13-5.15-2026-03` and nothing else

The device has **`CONFIG_MODVERSIONS=y`**, so the 235 prebuilt modules in `/vendor/lib/modules`
carry **symbol CRCs** that must match the kernel's. Those CRCs are stable within a **KMI
generation**, which is exactly what Google's dated branches encode. Reading every branch's
Makefile:

```
android13-5.15-2026-03   v5.15.197   <<< the only exact match
android13-5.15-2026-06   v5.15.206
android13-5.15-2026-09   v5.15.211
android13-5.15-lts       v5.15.216
```

Building from any other branch risks `disagrees about version of symbol` for every vendor module
→ dead Wi-Fi/camera/audio, or no boot at all.

Two further facts that make this work:
- **`# CONFIG_MODULE_SIG_FORCE is not set`** on the device → unsigned modules may load, which is
  what lets KernelSU/SUSFS work at all.
- With `CONFIG_MODVERSIONS=y` the kernel's `same_magic(..., has_crcs=true)` **skips the version
  token** of the vermagic and compares only the flag part, so the CRCs are the real gate. The
  workflow still pins `CONFIG_LOCALVERSION` for tidiness.

---

## Setting this up

1. Create a **new GitHub repository** (private is fine).
2. Put this folder's contents at the repo root, so the file lands at:
   `.github/workflows/build-kernel.yml`
3. Commit and push. (No git installed? You can also drag-and-drop the file into the repo via
   GitHub's web UI: **Add file → Upload files**, creating the `.github/workflows/` path.)

## Running the build

**Actions → Build GKI kernel (KernelSU-Next + SUSFS) → Run workflow.**

| Input | Default | Notes |
|---|---|---|
| `ksu_branch` | `dev` | `dev` is KernelSU-Next's default branch (newest tag is `v3.4.0` if you prefer a release) |
| `susfs_branch` | `gki-android13-5.15-dev` | both `gki-android13-5.15` (stable) and `-dev` (latest) are maintained — we chose **latest** |

**A note on mixing refs.** The SUSFS README recommends pinning *tags* because its patches target
specific KernelSU versions, and warns they "may differ for different kernel version or even on the
same kernel version". We deliberately run both at their branch tips instead. To make that
survivable rather than silent, the workflow now:

- **fails immediately** if the KernelSU-side patch doesn't wire SUSFS in — verified by grepping
  `KSU_SUSFS` in KernelSU-Next's own `kernel/Kconfig` (it declares only `KSU`, `KSU_DEBUG`,
  `KSU_DISABLE_MANAGER`, `KSU_DISABLE_POLICY`, `KSU_X86_PATCH_SYSCALL_DISPATCHER` — **no SUSFS**).
  Without the patch, kconfig silently drops `CONFIG_KSU_SUSFS` and you'd get a kernel whose SUSFS
  hooks go nowhere
- **fails** if no `*android13-5.15*.patch` exists on the chosen SUSFS branch

*(A custom manager signature hash for hiding can be added later — it needs a Kconfig change in
KernelSU-Next, so it was deliberately left out rather than exposed as a knob that does nothing.)*

Expect **~60–120 minutes**.

> **Run #1 failed in 1m44s with `No space left on device`** — and it took the *whole runner* down:
> `System.IO.IOException: ... /home/runner/actions-runner/cached/.../_diag/Worker_....log`.
> The runner's own logs live on `/`, and the stock `ubuntu-latest` image leaves `/` with very
> little free — so the failure happened before the build even started.
>
> The workflow now (a) reclaims space with the standard removals, (b) **measures both mounts and
> picks whichever has room** for `WORKDIR`, (c) **fails with an explicit error** if neither has
> ~40 GB, and (d) only uploads the small flashable files (the full dist contains `vmlinux`, which
> would blow both disk and artifact size).

### What it does, in order
1. Reclaim disk space, then **measure** `/` and `/mnt` and pick whichever has ≥40 GB for `WORKDIR`
2. `repo init/sync` the ACK at `common-android13-5.15-2026-03`
3. **Asserts** the source is exactly `5.15.197` (fails fast if not)
4. Integrates KernelSU-Next via its own `kernel/setup.sh`
5. **Applies SUSFS the documented three-part way** (this is what run #2 got wrong):
   - copies `kernel_patches/fs/*` → `common/fs/` and `kernel_patches/include/linux/*` →
     `common/include/linux/` — these provide `fs/susfs.c`, `susfs.h`, `susfs_def.h`
   - **does NOT** apply `kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch` — that patch is for
     *upstream* KernelSU and rejects against KernelSU-Next; SUSFS ships inside the KernelSU-Next SUSFS
     fork instead (see the run #7 note below). The step still asserts `KSU_SUSFS` is declared in
     KernelSU-Next's `kernel/Kconfig`, but that is now true because of the fork, not a patch.
   - applies the **kernel-side** patch `…/*android13-5.15*.patch`
6. **Re-applies every rejected hunk from its own content** (`.github/scripts/apply_rej.py`).
   Run #3's only reject was `fs/namespace.c` around line 32, and that hunk carries
   `#include <linux/susfs_def.h>`, two `extern`s and `#define CL_COPY_MNT_NS BIT(25)` — the applied
   SUSFS code *uses* `CL_COPY_MNT_NS`, so dropping that hunk guarantees a compile failure. The step
   now fails loudly if `CL_COPY_MNT_NS` is missing instead of discovering it two hours later.
7. Appends the option list from `.github/scripts/susfs.config` to `gki_defconfig`, and if kleaf's
   savedefconfig comparison rejects the result, adopts the canonical defconfig kleaf produced
   (see the note below)
8. **Config smoke test** — builds only `//common:kernel_aarch64_config` (~2 min, not ~2 h) and greps
   the generated `.config` for `CONFIG_KSU=y` and `CONFIG_KSU_SUSFS=y`, failing fast if absent
9. `tools/bazel build --config=fast --stamp //common:kernel_aarch64_dist`
10. Prints the dist listing and `kernelsu`/`susfs` markers, uploads `Image` / `Image.lz4` / `boot.img`

> **Runs #3–#6 all died at the config step on `ERROR: savedefconfig does not match …gki_defconfig`.**
> The pinned toolchain is the key: the manifest's `<default revision="master-kernel-build-2022">`
> means `build/kernel` (kleaf) is a **2022-era** revision, not `main`. Verified by downloading that
> branch's kleaf archive and grepping all 131 files: `--defconfig_fragment` does not exist, and
> **`check_defconfig` appears nowhere** — so there is no flag to pass and no attribute to flip.
>
> The guard is a bash **function** `check_defconfig` in `kernel/build`'s `_setup_env.sh` (which the
> manifest links to `build/_setup_env.sh`), `export -f`-ed so the ACK's config step can call it:
> ```bash
> function check_defconfig() {
>     (cd ${OUT_DIR} && make ${TOOL_ARGS} O=${OUT_DIR} savedefconfig)
>     diff -u .../configs/${DEFCONFIG} ${OUT_DIR}/defconfig >&2 || RES=$?
>     echo ERROR: savedefconfig does not match ... >&2
>     return ${RES}
> }
> ```
> It diffs the defconfig *file* against `make savedefconfig` output, which a hand-edited
> `gki_defconfig` can never satisfy: KernelSU-Next declares `config KSU` as `tristate … default y`,
> and `savedefconfig` never emits a value equal to its default — it drops `CONFIG_KSU=y` **even when
> the option is in force** (which is why run #3's diff showed `-CONFIG_KSU=y`; that did *not* mean
> KSU was off). `patch_setup_env.py` makes that function `return 0`, and the same step falls back to
> `tools/bazel run //common:kernel_aarch64_config` (which writes the minimized defconfig back into
> the tree) when the sandboxed `.config` isn't on disk.

> **RUN #7 — the real reason runs #3–#6 could never have produced a working SUSFS kernel.**
> `check_defconfig` was only the *first* wall. The second was architectural:
> **upstream `KernelSU-Next/KernelSU-Next` ships no SUSFS at all** (its `kernel/Kconfig` declares only
> `KSU`, `KSU_DEBUG`, `KSU_DISABLE_MANAGER`, `KSU_DISABLE_POLICY`, `KSU_X86_PATCH_SYSCALL_DISPATCHER`),
> and simonpunk's `10_enable_susfs_for_ksu.patch` is written for **upstream KernelSU (tiann)**. Against
> KernelSU-Next it rejects ~10 hunks — `kernel/Kbuild`, `kernel/core/init.c`, `kernel/feature/sucompat.c`,
> `kernel/hook/setuid_hook.c`, `kernel/supercall/*`, `kernel/feature/kernel_umount.c`,
> `kernel/supercall/dispatch.c` — and every one of those rejects is load-bearing.
>
> The kernel-side patch's `fs/exec.c` and `drivers/input/input.c` call these under `CONFIG_KSU_SUSFS=y`:
> ```c
> extern int ksu_handle_execveat(int *fd, struct filename **filename_ptr, void *argv, void *envp, int *flags);
> extern int ksu_handle_execveat_sucompat(int *fd, struct filename **, void *, void *, int *);
> extern int ksu_handle_post_execveat_sucompat(int *fd, struct filename **, void *, void *, int *, int *);
> extern __attribute__((cold)) int ksu_handle_input_handle_event(unsigned int *, unsigned int *, int *);
> extern struct static_key_true ksu_is_input_hook_enabled;
> ```
> KernelSU-Next has none of them (its `ksu_handle_execveat_sucompat` even has the *old*
> `(const char __user **, int, struct pt_regs *)` signature), so the run would have compiled for ~2 h
> and then **failed at link**. Worse, the old step's "decisive check" only grepped for `KSU_SUSFS` in
> the Kconfig — and the Kconfig hunk was one of the few that *did* apply, giving a **false green**.
>
> **The fix** is the one WildKernels use for their `next` flavor: build KernelSU-Next from the
> **SUSFS fork**, `github.com/pershoot/KernelSU-Next` branch **`dev-susfs`**. Its own `kernel/setup.sh`
> hardcodes `OWNER="pershoot"`, so only the `setup.sh` URL changed — the symlink + Kconfig/Makefile
> wiring is unchanged. WildKernels state the rule plainly in `.github/actions/susfs`:
> the enable patch is applied *only* when `root_flavor == 'kernelsu'`, because
> *"pershoot dev-susfs (next) … already include SUSFS, so this is a no-op"*. They also resolve
> `root_commit` from `https://github.com/pershoot/KernelSU-Next.git refs/heads/dev-susfs`, and their
> matrix lists our exact target (`sublevel 197` / `date 2026-03` for `android13-5.15`).
>
> Verified locally against a real sparse clone of `dev-susfs` — every symbol above is defined with the
> correct *new* signature (`feature/sucompat.c:289/277`, `runtime/ksud_integration.c:563/36`,
> `policy/allowlist.c:287`, `feature/sucompat.c:53`, `core/init.c:106`), the new-signature variants sit
> inside `#ifdef CONFIG_KSU_SUSFS`, and the 17 `susfs_*` helpers it calls that simonpunk's headers don't
> declare are pershoot's *own* `selinux/selinux.c` symbols — so there is **no gap in either direction**.
> `CONFIG_KSU_SUSFS_SUS_MAP=y` was added to `susfs.config` because `dev-susfs` references it.
> Both directions of the API contract are now asserted in the workflow, so a future mismatch fails in
> ~2 min instead of ~2 h.

> The SUSFS README is explicit that "if there are failed patches, you may try to patch them
> manually by yourself" — so rejects are expected here, not a bug. Steps 5+6 make that automatic.

### Reading the result
The artifact `kernel-Image-<run>` should contain `Image`, `Image.lz4`, and possibly a ready-made
`boot.img` from the dist. **Read the step log first** — the "dist listing" and marker lines tell
you whether SUSFS actually got applied.

---

## Flashing it (the kernel lives in `boot_a`)

`vendor_boot` and `init_boot` stay **stock** — only `boot` carries the kernel, so it is the only
partition this build replaces.

### Path 1 — if the dist produced a `boot.img`
```cmd
set PT=C:\Users\coolk\Downloads\platform-tools
"%PT%\fastboot.exe" flash boot_a <downloaded-boot.img>
"%PT%\fastboot.exe" reboot
```

### Path 2 — repack the kernel into the stock boot image
The stock boot image stores a **compressed** kernel, and kleaf emits exactly that as `Image.lz4`.

**On the phone (no Windows tooling needed — you already have root):** use MagiskBoot.
```cmd
"%PT%\adb.exe" push Image.lz4 /data/local/tmp/
"%PT%\adb.exe" shell su -c "cd /data/local/tmp && magiskboot unpack /dev/block/by-name/boot_a && cp Image.lz4 kernel && magiskboot repack /dev/block/by-name/boot_a new-boot.img && dd if=new-boot.img of=/dev/block/by-name/boot_a"
```
*(MagiskBoot can be taken from any AnyKernel3 zip or the Magisk APK's `lib/arm64-v8a/`.)*

**On Windows:** install Python (`winget install Python.Python.3.12`), then use AOSP's
`unpack_bootimg` / `mkbootimg`, replacing `kernel` with `Image.lz4` and preserving the header.

> ⚠️ Prefer Path 1 or the on-device route — both preserve the boot header exactly. Hand-repacking
> has header-version pitfalls (the stock image uses a modern GKI header with OS/security-patch
> fields that must be carried over verbatim).

---

## Verify AFTER flashing — this is the step that matters

```cmd
set PT=C:\Users\coolk\Downloads\platform-tools
"%PT%\adb.exe" shell getprop ro.boot.verifiedbootstate
"%PT%\adb.exe" shell getprop ro.boot.flash.locked
"%PT%\adb.exe" shell getprop ro.boot.vbmeta.device_state
"%PT%\adb.exe" shell uname -r
"%PT%\adb.exe" shell su -c id

:: THE critical one - vendor modules must load
"%PT%\adb.exe" shell su -c "dmesg | grep -iE 'disagrees about version|module verification failed|Unknown symbol'"
```

| Check | Expected |
|---|---|
| `verifiedbootstate` / `flash.locked` / `device_state` | `green` / `1` / `locked` — Fenrir's LK spoof is untouched by `boot`, so it must persist |
| `uname -r` | should read `5.15.197-…` — the same lineage |
| `su -c id` | `uid=0(root)` |
| **the grep above** | **no output.** Any hits mean a KMI/CRC mismatch → roll back |
| Wi-Fi / camera / audio | must all still work (they are vendor modules) |
| Google Wallet | add a card / tap — it should still work |

Also confirm SUSFS is alive — the KernelSU-Next manager shows **SuSFS Controls & Info**.

---

## Rollback

Keep the stock boot image ready at all times. Yours is preserved at:
`C:\Users\coolk\workspace\nothing_stock_baseline\flash_tool_readback\ReadBack_…_boot_a_boot.img`

```cmd
"%PT%\fastboot.exe" flash boot_a "C:\Users\coolk\workspace\nothing_stock_baseline\flash_tool_readback\ReadBack_AUQCBY4DHID6ZLQG_boot_a_boot.img"
"%PT%\fastboot.exe" reboot
```
That is a 20-second recovery with no data loss → **zero reason to tolerate a misbehaving kernel.**

---

## Things worth knowing

- **`/vendor/lib/modules` must keep loading.** That is the single most likely failure mode and the
  reason the source branch is pinned so precisely.
- **LTO mode does not affect CRCs or vermagic**, so `--config=fast` is safe and much faster than a
  full LTO build.
- Your previous CI failed on `undefined symbol: ksu_handle_devpts` cascading into
  `pahole: .tmp_vmlinux.btf: No such file or directory`. That was a KernelSU devpts-hook patch
  mismatch, not a toolchain problem — the workflow above uses KernelSU-Next's own integrator plus a
  **dry-run** of the SUSFS patch so a bad hunk fails loudly instead of mangling the tree.
- SUSFS requires kernel-side code, which is why an LKM/init_boot-only install cannot provide it.
- The **Fenrir preloader image is not currently backed up locally** (only the stock one). It is
  already flashed on the phone so nothing is at risk, but if you ever need to re-flash Fenrir,
  re-download the release from the archived repo.

