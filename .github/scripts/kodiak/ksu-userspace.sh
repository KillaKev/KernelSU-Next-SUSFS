#!/usr/bin/env bash
# pixel11 CI: resolve the KernelSU-Next userspace that MATCHES this kernel, and gate on it.
#
# WHY THIS EXISTS (2026-10-04, run #38)
#   KernelSU's kernel<->userspace contract is KERNEL_SU_UAPI_VERSION. This kernel is built from
#   pershoot/KernelSU-Next dev-susfs, whose uapi had just moved 4 -> 5, while the phone - and,
#   worse, this workflow's own AnyKernel3 payload (a hard-pinned v3.4.0 release APK, uapi 4) -
#   carried 4. The result: no `su`, no root, and no dmesg to explain it, for a whole evening.
#   Root came back the moment a uapi-5 ksud was installed. So the pair is resolved here, from
#   SOURCE files only (nothing aarch64 is executed on this x86 runner), and a disagreement stops
#   the build before the two-hour compile rather than after the flash.
#
# EXIT CODES:  0 = gate passed (or could not be evaluated - warnings printed), 1 = uapi MISMATCH.
# The mismatch case is the whole point: better a red build than an unrootable kernel.
set -o pipefail
UP="KernelSU-Next/KernelSU-Next"

KAPI="${GITHUB_WORKSPACE}/kernel/KernelSU-Next/uapi/supercall.h"
test -f "$KAPI" || { echo "::error::$KAPI is missing - cannot read the kernel's uapi"; exit 1; }
KUI="$(grep -oE 'KERNEL_SU_UAPI_VERSION[[:space:]]*=[[:space:]]*[0-9]+' "$KAPI" | grep -oE '[0-9]+' | head -1 || true)"
case "$KUI" in
  ''|*[!0-9]*) echo "::error::cannot parse KERNEL_SU_UAPI_VERSION from $KAPI"
               grep -n 'KERNEL_SU_UAPI_VERSION' "$KAPI" || true
               exit 1 ;;
esac
echo "kernel KERNEL_SU_UAPI_VERSION = $KUI"

command -v jq >/dev/null || { echo "::error::jq is missing on this runner"; exit 1; }
if ! RUNS="$(curl -fsSL -H 'User-Agent: kodiak-ci' \
              "https://api.github.com/repos/$UP/actions/runs?branch=dev&per_page=30")"; then
  echo "::warning::cannot reach the GitHub API - the userspace gate is skipped for this run"
  exit 0
fi
SEL='[.workflow_runs[] | select(.path | test("build-manager-ci")) | select(.conclusion == "success")][0]'
CI_ID="$(printf '%s' "$RUNS"   | jq -r "$SEL.id // empty")"
CI_SHA="$(printf '%s' "$RUNS"  | jq -r "$SEL.head_sha // empty")"
CI_DATE="$(printf '%s' "$RUNS" | jq -r "$SEL.created_at // empty")"
CI_URL="$(printf '%s' "$RUNS"  | jq -r "$SEL.html_url // empty")"
if [ -z "$CI_SHA" ]; then
  echo "::warning::no successful Build Manager CI run found - gate skipped, no userspace shipped"
  exit 0
fi
echo "latest CI run $CI_ID  sha $CI_SHA  $CI_DATE"

CIU="$(curl -fsSL "https://raw.githubusercontent.com/$UP/$CI_SHA/uapi/supercall.h" \
       | grep -oE 'KERNEL_SU_UAPI_VERSION[[:space:]]*=[[:space:]]*[0-9]+' | grep -oE '[0-9]+' | head -1 || true)"
echo "CI     KERNEL_SU_UAPI_VERSION = ${CIU:-unreadable}"
if [ -z "$CIU" ]; then
  echo "::warning::could not read the CI userspace's uapi - gate skipped, no userspace shipped"
  exit 0
fi
if [ "$CIU" != "$KUI" ]; then
  echo "::error::uapi MISMATCH - this kernel declares $KUI, the latest KernelSU-Next CI userspace declares $CIU"
  echo "::error::flashing this kernel as-is would leave the phone without su (exactly the 2026-10-04 outage)."
  echo "::error::fix: set ksu_branch to the dev-susfs commit whose uapi/supercall.h says $CIU, or wait for CI to move to $KUI."
  exit 1
fi
echo "ok: this kernel and the latest CI userspace agree on uapi $KUI - the pair can ship"

{
  echo "KSU_UAPI=$KUI"
  echo "KSU_CI_ID=$CI_ID"
  echo "KSU_CI_SHA=$CI_SHA"
  echo "KSU_CI_DATE=$CI_DATE"
  echo "KSU_CI_URL=$CI_URL"
  echo "KSU_CI_UAPI=$CIU"
} >> "${GITHUB_ENV:-/dev/null}"

# --------------------------------------------------------------------------------------------
# Best-effort download of THAT EXACT userspace. Best effort on purpose:
#   * GitHub requires a token even for a public repo's artifact zip (401 anonymous; 403 for a
#     fine-grained PAT that has no Actions:read on that repo), so this needs a KSU_CI_TOKEN
#     secret. Without it the run continues and the summary names the CI run to install from.
#   * what must NEVER happen is a fallback to an OLDER userspace. That is the mismatch this whole
#     step exists to prevent, so a failed download ships the Image alone and says so.
#   * WITH a KSU_CI_TOKEN secret, delivery is a promise: a missing or undownloadable artifact fails
#     the run instead of warning, because a silent "no APK" is indistinguishable from success.
# --------------------------------------------------------------------------------------------
UD="${GITHUB_WORKSPACE}/userspace"
mkdir -p "$UD" "${GITHUB_WORKSPACE}/artifacts"

ARTS="$(curl -fsSL -H 'User-Agent: kodiak-ci' \
         "https://api.github.com/repos/$UP/actions/runs/$CI_ID/artifacts" || true)"
printf '%s' "$ARTS" | jq -r '.artifacts[]? | select(.expired == false) | "\(.name) \(.id) \(.size_in_bytes)"' \
  > "$UD/artifacts.txt" || true
echo "artifacts in run $CI_ID that matter here:"
grep -E '^(manager-spoofed|manager|mappings-spoofed|ksud-aarch64-linux-android) ' "$UD/artifacts.txt" \
  || echo "   (none listed)"

TOK="${KSU_CI_TOKEN:-}"
if [ -z "$TOK" ]; then
  echo "::warning::no KSU_CI_TOKEN secret - the matching userspace is NOT shipped; install 'manager-spoofed' from $CI_URL by hand (it carries uapi $CIU)"
  exit 0
fi

# A TOKEN IS A PROMISE. With KSU_CI_TOKEN set, a missing or undownloadable artifact is a hard
# failure, not a warning: the point of the secret is that the run DELIVERS the userspace, and
# degrading quietly to "no APK" is exactly the silent behaviour that cost the 2026-10-04 flash its
# root. Without the secret the code above has already exited, so this only fires when delivery was
# asked for and did not happen.
for want in manager-spoofed mappings-spoofed; do
  ID="$(awk -v n="$want" '$1 == n { print $2 }' "$UD/artifacts.txt" | head -1 || true)"
  if [ -z "$ID" ]; then
    echo "::error::artifact $want is not in CI run $CI_ID, but a KSU_CI_TOKEN is set - the pair was expected"
    exit 1
  fi
  if curl -fsSL -H "Authorization: token $TOK" -H 'User-Agent: kodiak-ci' \
       "https://api.github.com/repos/$UP/actions/artifacts/$ID/zip" -o "$UD/$want.zip"; then
    echo "downloaded $want ($(stat -c%s "$UD/$want.zip") bytes)"
  else
    echo "::error::cannot download $want with the KSU_CI_TOKEN provided - a classic PAT needs the repo scope, a fine-grained one needs Actions:read on $UP (has it expired or been revoked?)"
    exit 1
  fi
done

if [ ! -f "$UD/manager-spoofed.zip" ]; then
  echo "::error::manager-spoofed is not on disk - this run would ship the Image only"
  exit 1
fi
mkdir -p "$UD/manager-spoofed"
( cd "$UD/manager-spoofed" && unzip -o "$UD/manager-spoofed.zip" >/dev/null )
APK="$(find "$UD/manager-spoofed" -type f -iname '*.apk' | head -1 || true)"
if [ -z "$APK" ]; then
  APK="$(find "$UD/manager-spoofed" -type f | head -1 || true)"
  cp -f "$APK" "$UD/manager.apk"
  APK="$UD/manager.apk"
fi
if [ -z "$APK" ] || [ ! -f "$APK" ]; then
  echo "::error::the manager-spoofed artifact holds no file - there is nothing to ship"
  exit 1
fi
cp -f "$APK" "${GITHUB_WORKSPACE}/artifacts/${FILE_NAME}-manager-spoofed.apk"
echo "shipping ${FILE_NAME}-manager-spoofed.apk from CI run $CI_ID ($(stat -c%s "$APK") bytes)"

# ...and its companion. The spoofed manager is only half the pair: mappings-spoofed is the table it
# uses in place of the stock one. It was downloaded above (and its absence is now a hard failure
# when the token is set), so it is delivered here too and the artifacts stay self-contained.
if [ -f "$UD/mappings-spoofed.zip" ]; then
  cp -f "$UD/mappings-spoofed.zip" "${GITHUB_WORKSPACE}/artifacts/${FILE_NAME}-mappings-spoofed.zip"
  echo "shipping ${FILE_NAME}-mappings-spoofed.zip from CI run $CI_ID ($(stat -c%s "$UD/mappings-spoofed.zip") bytes)"
fi

# ksud is embedded in the manager APK as lib/arm64-v8a/libksud.so. The AnyKernel3 step copies
# userspace/ksud into the zip and ksu-install.sh installs it to /data/adb/ksud during the flash -
# guarded, so a missing file or a read-only /data can never fail the kernel flash itself.
if unzip -o -j "$APK" 'lib/arm64-v8a/libksud.so' -d "$UD/ksud.d" >/dev/null 2>&1 \
   && [ -f "$UD/ksud.d/libksud.so" ]; then
  cp -f "$UD/ksud.d/libksud.so" "$UD/ksud"
  chmod 0755 "$UD/ksud"
  echo "ksud lifted out of the APK: $(stat -c%s "$UD/ksud") bytes, sha256 $(sha256sum "$UD/ksud" | awk '{print $1}')"
else
  echo "::warning::that manager artifact has no lib/arm64-v8a/libksud.so - ksud will not be installed at flash time"
fi
exit 0
