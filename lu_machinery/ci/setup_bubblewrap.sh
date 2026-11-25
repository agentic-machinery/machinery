#!/usr/bin/env bash
# Verify the pinned bubblewrap archive and install it on a disposable CI VM.
# --package-only prepares the archive for the default branch's Actions cache.
set -euo pipefail

: "${RUNNER_TEMP:?RUNNER_TEMP must be set}"

read_pin() {
  awk -F= -v key="$1" '
    $1 == key { count++; value = substr($0, index($0, "=") + 1) }
    END { if (count != 1) exit 1; print value }
  ' "$pins"
}

download_bubblewrap() {
  local snapshot_url="$1" destination="$2" digest="$3" base
  local deb="${destination##*/}"
  for base in "$snapshot_url" https://security.ubuntu.com/ubuntu; do
    # curl backs off on transient errors. Cap each source, including Retry-After.
    if timeout --kill-after=1 14 \
      curl -q -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 4 --max-time 5 --retry 2 \
      -o "$destination" "${base}/pool/main/b/bubblewrap/${deb}"; then
      if ! printf '%s  %s\n' "$digest" "$destination" | sha256sum --check --status; then
        echo "::error::SHA-256 verification failed for ${deb} from ${base}" >&2
        return 1
      fi
      return 0
    fi
    echo "::warning::Could not fetch ${deb} from ${base} within the download limit" >&2
  done
  echo "::error::Could not fetch ${deb} from either official Ubuntu source" >&2
  return 1
}

prepare_bubblewrap_package() {
  local snapshot_url="$1" archive="$2" deb="$3" digest="$4"
  local cache_dir="${archive%/*}"
  if [ -e "$archive" ] || [ -L "$archive" ]; then
    if [ -L "$archive" ] || [ ! -f "$archive" ] ||
      ! printf '%s  %s\n' "$digest" "$archive" | sha256sum --check --status; then
      echo "::error::Cached bubblewrap archive is not a regular file with the pinned SHA-256" >&2
      return 1
    fi
    echo "Verified cached bubblewrap package ${deb}"
    return 0
  fi
  if [ "${BUBBLEWRAP_CACHE_HIT:-}" = true ]; then
    echo "::error::The bubblewrap cache reported a hit without the pinned archive" >&2
    return 1
  fi

  (
    mkdir -p -- "$cache_dir"
    scratch="$(mktemp -d -- "$cache_dir/.download.XXXXXXXX")"
    trap 'rm -rf -- "$scratch"' EXIT
    download_bubblewrap "$snapshot_url" "$scratch/$deb" "$digest" || exit
    mv -T -- "$scratch/$deb" "$archive"
    echo "Verified downloaded bubblewrap package ${deb}"
  )
}

# dpkg has no lock timeout. Retry only lock failures inside the caller's existing
# process cap, leaving two seconds for the final attempt and five seconds kill grace.
dpkg_with_lock_wait() {
  local cap="$1"
  shift
  sudo timeout --kill-after=5 "$cap" env LC_ALL=C bash -c '
    deadline=$((SECONDS + $1 - 2))
    shift
    while true; do
      err=$(dpkg "$@" 2>&1) && { printf "%s\n" "$err"; exit 0; }
      rc=$?
      printf "%s\n" "$err" >&2
      case "$err" in
        *"was locked by another process"*) ;;
        *) exit "$rc" ;;
      esac
      [ "$SECONDS" -lt "$deadline" ] || exit "$rc"
      sleep 1
    done
  ' _ "$cap" "$@"
}

if (( $# > 1 )) || { (( $# == 1 )) && [ "$1" != --package-only ]; }; then
  echo "::error::Usage: setup_bubblewrap.sh [--package-only]" >&2
  exit 1
fi

code_root="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
pins="$code_root/.github/workflows/pins.txt"
case "$code_root" in
  */lib/python*/site-packages|*/lib64/python*/site-packages)
    pins="${code_root%/*/*/*}/share/lu-machinery/ci/pins.txt"
    ;;
esac

if ! snapshot="$(read_pin CI_UBUNTU_SNAPSHOT)" || ! [[ "$snapshot" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
  echo "::error::Expected one CI_UBUNTU_SNAPSHOT in UTC (YYYYMMDDTHHMMSSZ) in ${pins}" >&2
  exit 1
fi
if ! deb="$(read_pin CI_BUBBLEWRAP_DEB)" || ! [[ "$deb" =~ ^bubblewrap_[0-9][a-z0-9.+~-]*_amd64\.deb$ ]]; then
  echo "::error::Expected one CI_BUBBLEWRAP_DEB naming a versioned amd64 bubblewrap package in ${pins}" >&2
  exit 1
fi
if ! digest="$(read_pin CI_BUBBLEWRAP_SHA256)" || ! [[ "$digest" =~ ^[0-9a-f]{64}$ ]]; then
  echo "::error::Expected one CI_BUBBLEWRAP_SHA256 with 64 lowercase hex digits in ${pins}" >&2
  exit 1
fi

snapshot_url="https://snapshot.ubuntu.com/ubuntu/${snapshot}"
archive="$RUNNER_TEMP/release-cache/bubblewrap/archive.deb"
prepare_bubblewrap_package "$snapshot_url" "$archive" "$deb" "$digest"

if [ "${1:-}" = --package-only ]; then
  exit 0
fi

# The Ubuntu image supplies this archive's shared libraries and the AppArmor
# parser. dpkg installs only the verified file and has no archive or index access.
dpkg_with_lock_wait 120 --install -- "$archive"

# Ubuntu-hosted runners require an application-specific userns grant. This
# profile belongs only to the disposable CI VM.
if [ -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]; then
  cat > "${RUNNER_TEMP}/v4-ci-bwrap.apparmor" <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>
profile v4_ci_bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
}
PROFILE
  sudo apparmor_parser -r "${RUNNER_TEMP}/v4-ci-bwrap.apparmor"
fi
/usr/bin/bwrap --unshare-user --unshare-pid --ro-bind / / -- /usr/bin/true
