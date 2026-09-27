#!/usr/bin/env bash
# ============================================================================
# verify_kernel_image.sh <image> <expected-release-prefix> <patches-mode>
#
# Answers ONE question with only the artifact in hand: is this the kernel we
# think we built? Every check here exists because a cheaper signal ("the build
# succeeded") has lied before on the phone-2a pipeline:
#
#   * a kernel built from the wrong dated branch still builds cleanly, it just
#     carries the wrong release string (and, if it is a different KMI, the wrong
#     CRCs);
#   * kleaf's stamp machinery happily appends "-maybe-dirty", which ends up in
#     UTS_RELEASE and therefore in the vermagic the device and its attestation
#     stack see;
#   * a SUSFS patch that failed to apply leaves a kernel that boots and looks
#     normal while hiding nothing.
#
# Exit 0 only when all applicable checks pass.
# ============================================================================
set -uo pipefail

IMG="${1:-}"
PREFIX="${2:-}"
MODE="${3:-ksu-susfs}"

if [ -z "$IMG" ] || [ ! -f "$IMG" ]; then
  echo "::error::verify_kernel_image.sh: no such Image: '$IMG'"
  exit 1
fi

fail=0
err() { echo "::error::$*"; fail=1; }

echo "===== verifying $IMG ====="
SIZE=$(stat -c %s "$IMG")
echo "size: $SIZE bytes"
if [ "$SIZE" -lt 10000000 ]; then
  err "Image is only ${SIZE} bytes - that is not a kernel (expected >10MB, typically ~40MB)"
fi

# --- arm64 Image magic: 0x644d5241 ("ARMd") at offset 0x38 -------------------
MAGIC=$(od -An -tx1 -j56 -N4 "$IMG" | tr -d ' \n')
echo "arm64 image magic at 0x38: ${MAGIC:-<nothing>}  (expect 41524d64)"
if [ "$MAGIC" != "41524d64" ]; then
  err "not an aarch64 Image (bad magic) - did the build emit a compressed or gzipped artifact?"
fi

# --- release string ---------------------------------------------------------
echo "--- release strings found ---"
VER=$(strings "$IMG" | grep -m1 '^Linux version ' || true)
if [ -z "$VER" ]; then
  err "no 'Linux version' string in the Image"
else
  echo "$VER"
  UTS=$(echo "$VER" | awk '{print $3}')
  echo "UTS_RELEASE: $UTS"
  if [ -n "$PREFIX" ] && [[ "$UTS" != "$PREFIX"* ]]; then
    err "release string '$UTS' does not start with the expected '$PREFIX'"
    echo "::error::that means the branch/sublevel is not the one this repo pins - check expected_sublevel"
  fi
  case "$UTS" in
    *dirty*|*maybe-dirty*)
      err "release string contains a dirty marker ('$UTS') - kleaf's LOCALVERSION / setlocalversion was not neutralised"
      ;;
  esac
fi
# Every string that looks like a release of this line (for the human reading the log)
strings "$IMG" | grep -E '^6\.12\.[0-9]+-android1[567]-' | sort -u | head -5 || true

# --- KernelSU / SUSFS markers ----------------------------------------------
echo "--- KernelSU / SUSFS markers ---"
SUSFS_HITS=$(strings "$IMG" | grep -ci 'susfs' || true)
KSU_HITS=$(strings -a "$IMG" | grep -ciE 'kernelsu|kernelsu-next' || true)
echo "strings matching 'susfs'    : ${SUSFS_HITS}"
echo "strings matching 'kernelsu' : ${KSU_HITS}"
strings "$IMG" | grep -iE 'susfs|kernelsu' | sort -u | head -12 || true

case "$MODE" in
  ksu-susfs)
    [ "${SUSFS_HITS:-0}" -ge 1 ] || err "patches=ksu-susfs but the Image contains NO 'susfs' string - SUSFS was not compiled in"
    [ "${KSU_HITS:-0}"   -ge 1 ] || err "patches=ksu-susfs but the Image contains NO 'kernelsu' string - KernelSU was not compiled in"
    ;;
  ksun-only)
    [ "${KSU_HITS:-0}"   -ge 1 ] || err "patches=ksun-only but the Image contains NO 'kernelsu' string"
    [ "${SUSFS_HITS:-0}" -eq 0 ] || err "patches=ksun-only but the Image contains SUSFS strings - SUSFS leaked in"
    ;;
  none)
    # Vanilla control: a control that carries the patches is not a control.
    [ "${SUSFS_HITS:-0}" -eq 0 ] || err "patches=none (VANILLA CONTROL) but the Image contains SUSFS strings"
    [ "${KSU_HITS:-0}"   -eq 0 ] || err "patches=none (VANILLA CONTROL) but the Image contains KernelSU strings"
    ;;
  *)
    err "unknown patches mode '$MODE'"
    ;;
esac

echo "===== result ====="
if [ "$fail" -ne 0 ]; then
  echo "::error::verification FAILED - do not flash this artifact"
  exit 1
fi
echo "OK - release string, arm64 magic and patch markers all consistent with mode '$MODE'"
