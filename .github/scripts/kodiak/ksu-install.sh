# pixel11 CI: install the matching KernelSU userspace (ksud) during the AnyKernel3 flash.
#
# WHY (run #24-#26 lesson): our kernel reports KernelSU-Next 33318 (v3.4.0) while the phone
# carried ksud 3.3.0-41-g39ba3821. The kernel's version handshake fails, `su` never appears, and
# without root the kernel's module-refusal messages (dmesg) are unreadable - which is what blocked
# the module diagnosis. WildKernels' AK3 ships the KSU userspace; ours only shipped the Image.
#
# HOW: the workflow extracts ksud from the pinned manager APK (it is embedded there as
# lib/arm64-v8a/libksud.so, hash-verified) and puts it next to anykernel.sh. This fragment is then
# PREPENDED to anykernel.sh, right after its shebang, so it always runs before the AK3 flow and
# cannot be skipped by an early exit. Every command is guarded, so a read-only /data (recovery)
# can never fail the kernel flash itself.
(
  SRC=""
  for d in "$(dirname "$0")" "$AKHOME" "$PWD" "$(dirname "$0")/.."; do
    if [ -n "$d" ] && [ -f "$d/ksud" ]; then SRC="$d/ksud"; break; fi
  done
  if [ -z "$SRC" ]; then
    echo "- ksud not found next to anykernel.sh - skipping KernelSU userspace install"
  elif [ ! -d /data/adb ]; then
    echo "- /data/adb unavailable (recovery?) - skipping KernelSU userspace install"
  else
    mkdir -p /data/adb/ksu/bin /data/adb/ksu/lib
    cp -f "$SRC" /data/adb/ksud
    chmod 0755 /data/adb/ksud
    chown 0:0 /data/adb/ksud 2>/dev/null
    if [ -x /system/bin/restorecon ]; then restorecon /data/adb/ksud; fi
    echo "- installed matching KernelSU userspace -> /data/adb/ksud"
  fi
) 2>/dev/null || true
# --- end pixel11 CI ksud install ---
