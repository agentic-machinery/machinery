#!/usr/bin/env bash
# Install bubblewrap and grant user namespaces on disposable CI VMs only.
# Optional package names share the APT transaction with the pinned bubblewrap build.
# Installed packages are reused. Missing packages come from one signed Ubuntu snapshot.
set -euo pipefail

: "${RUNNER_TEMP:?RUNNER_TEMP must be set}"

# True when /var/lib/dpkg/lock is held by a POSIX write lock. dpkg uses fcntl
# (F_SETLK); flock(1) does not contend with that lock and is not a probe.
# /proc/locks prints the id as major:minor:inode with the same hex width as printf %02x.
dpkg_database_lock_held() {
  local dev inode major minor token
  dev=$(stat -c %d /var/lib/dpkg/lock 2>/dev/null) || return 1
  inode=$(stat -c %i /var/lib/dpkg/lock 2>/dev/null) || return 1
  # glibc major()/minor() for dev_t. High major bits stay 0 on runner disks.
  major=$(( (dev & 0xfff00) >> 8 ))
  minor=$(( (dev & 0xff) | ((dev >> 12) & 0xffffff00) ))
  printf -v token '%02x:%02x:%s' "$major" "$minor" "$inode"
  awk -v token="$token" '
    $2 == "POSIX" && $4 == "WRITE" && $6 == token { found = 1 }
    END { exit !found }
  ' /proc/locks
}

# apt-get does not inherit the `apt` binary's Dpkg::Lock::Timeout (120 on a
# non-tty). timeout(1) then kills the wait and leaves no lock text, which this
# script used to treat as a mirror failure.
apt_failure_is_dpkg_lock() {
  local rc="$1" log="$2"
  if grep -E -q '^(E: (Could not get lock|Unable to acquire the dpkg frontend lock|Unable to lock)|dpkg: error: .*was locked by another process)' "$log"; then
    return 0
  fi
  # 124: timeout sent TERM. 137: the command needed KILL.
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    dpkg_database_lock_held
    return
  fi
  return 1
}

# dpkg has no lock timeout. Retry only lock failures inside the caller's existing
# process cap, leaving two seconds for the final attempt and five seconds kill grace.
dpkg_with_lock_wait() {
  local cap="$1"
  shift
  sudo timeout --kill-after=5 "$cap" bash -c '
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

apt_install() {
  local cap="$1" lock_wait="$2" rc=0
  shift 2
  # The runner owns the log under RUNNER_TEMP.
  # shellcheck disable=SC2024
  sudo timeout --kill-after=5 "$cap" env -u APT_CONFIG LC_ALL=C apt-get "${apt_opts[@]}" \
    -o "DPkg::Lock::Timeout=$lock_wait" install -y --no-install-recommends --no-remove -- "$@" \
    >"$install_log" 2>&1 || rc=$?
  cat "$install_log" || return "$?"
  if [ "$rc" -ne 0 ]; then
    echo "apt-get install from Ubuntu snapshot ${snapshot} failed (exit ${rc})" >&2
  fi
  return "$rc"
}

apt_reports_interrupted_dpkg() {
  grep -Fq 'E: dpkg was interrupted' "$install_log"
}

offline_apt_install() {
  local deb_path="$1" log="$2" lock_wait="${3:-25}" rc=0
  local -a offline_opts=(
    -o Dir::Etc::sourcelist=/dev/null
    -o Dir::Etc::sourceparts=/dev/null
    -o "Dir::State::Lists=$apt_dir/lists"
    -o "Dir::Cache::Archives=$apt_dir/archives"
    -o Dir::Cache::pkgcache=
    -o Dir::Cache::srcpkgcache=
    -o Acquire::Languages=none
  )
  # The runner owns the log under /tmp. Empty sources/lists guarantee no network
  # download can occur while still allowing APT's local .deb acquire path (APT's
  # --no-download fails on local .deb arguments with a non-absolute pathname error).
  # shellcheck disable=SC2024
  sudo timeout --kill-after=5 35 env -u APT_CONFIG LC_ALL=C apt-get "${offline_opts[@]}" \
    -o "DPkg::Lock::Timeout=$lock_wait" install -y --no-install-recommends --no-remove -- "$deb_path" \
    >"$log" 2>&1 || rc=$?
  cat "$log" || return "$?"
  return "$rc"
}

# Obtain the pinned bubblewrap .deb, always verifying SHA-256 against the pin
# before use. Prefer a restored cache file under RUNNER_TEMP/release-cache;
# reject and evict any non-regular or checksum-mismatched cache file and fall
# back to the pinned Ubuntu snapshot with bounded retries and exponential backoff.
fetch_verified_bubblewrap_deb() {
  local deb="$1" digest="$2" snapshot="$3" snapshot_url="$4" apt_dir="$5"
  local cache_root="$RUNNER_TEMP/release-cache"
  local cache_dir="$cache_root/bubblewrap"
  local cached_deb="$cache_dir/$deb"
  local url="${snapshot_url}/pool/main/b/bubblewrap/${deb}"
  local max_attempts=5
  local fetch_deadline=$((SECONDS + 45))
  local attempt=0 backoff=2 remaining curl_max connect_timeout wait_s cache_tmp

  if [ -e "$cached_deb" ] || [ -L "$cached_deb" ]; then
    if [ ! -L "$cache_root" ] && [ ! -L "$cache_dir" ] && [ -f "$cached_deb" ] && [ ! -L "$cached_deb" ]; then
      rm -f -- "$apt_dir/$deb"
      if cp -- "$cached_deb" "$apt_dir/$deb" && [ -f "$apt_dir/$deb" ] && [ ! -L "$apt_dir/$deb" ] &&
        echo "${digest}  $apt_dir/$deb" | sha256sum -c -; then
        echo "Using cached verified ${deb}"
        return 0
      fi
      rm -f -- "$apt_dir/$deb"
      echo "::warning::Cached ${deb} failed SHA-256 verification; rejecting and refetching from Ubuntu snapshot ${snapshot}" >&2
    else
      echo "::warning::Ignoring non-regular cached ${deb} path; fetching from Ubuntu snapshot ${snapshot}" >&2
    fi
    if [ ! -L "$cache_root" ] && [ ! -L "$cache_dir" ] && [ -d "$cache_dir" ]; then
      rm -rf -- "$cached_deb"
    fi
  fi

  while [ "$attempt" -lt "$max_attempts" ]; do
    attempt=$((attempt + 1))
    remaining=$((fetch_deadline - SECONDS))
    if [ "$remaining" -lt 1 ]; then
      break
    fi
    curl_max=15
    if [ "$remaining" -lt "$curl_max" ]; then
      curl_max="$remaining"
    fi
    connect_timeout=4
    if [ "$curl_max" -lt "$connect_timeout" ]; then
      connect_timeout="$curl_max"
    fi
    rm -f -- "$apt_dir/$deb"
    if curl -q -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 \
      --connect-timeout "$connect_timeout" --max-time "$curl_max" \
      -o "$apt_dir/$deb" "$url"; then
      if [ -f "$apt_dir/$deb" ] && [ ! -L "$apt_dir/$deb" ] &&
        echo "${digest}  $apt_dir/$deb" | sha256sum -c -; then
        if [ ! -L "$cache_root" ] && [ ! -L "$cache_dir" ] &&
          { [ ! -e "$cache_root" ] || [ -d "$cache_root" ]; } &&
          { [ ! -e "$cache_dir" ] || [ -d "$cache_dir" ]; } &&
          mkdir -p -- "$cache_dir"; then
          if cache_tmp="$(mktemp "$cache_dir/.${deb}.XXXXXXXX")"; then
            if cp -- "$apt_dir/$deb" "$cache_tmp" && [ ! -L "$cache_dir" ]; then
              rm -rf -- "$cached_deb"
              mv -f -- "$cache_tmp" "$cached_deb" || rm -f -- "$cache_tmp"
            else
              rm -f -- "$cache_tmp"
            fi
          fi
        fi
        return 0
      fi
      rm -f -- "$apt_dir/$deb"
      echo "::warning::Fetched ${deb} from Ubuntu snapshot ${snapshot} failed SHA-256 verification (attempt ${attempt}/${max_attempts})" >&2
    else
      rm -f -- "$apt_dir/$deb"
      echo "::warning::Could not fetch ${deb} from Ubuntu snapshot ${snapshot} (attempt ${attempt}/${max_attempts})" >&2
    fi
    remaining=$((fetch_deadline - SECONDS))
    if [ "$attempt" -ge "$max_attempts" ] || [ "$remaining" -le 1 ]; then
      break
    fi
    wait_s="$backoff"
    if [ "$wait_s" -ge "$remaining" ]; then
      wait_s=$((remaining - 1))
    fi
    echo "Retrying ${deb} fetch in ${wait_s}s..." >&2
    sleep "$wait_s"
    backoff=$((backoff * 2))
  done
  rm -f -- "$apt_dir/$deb"
  echo "::error::Could not fetch verified ${deb} from Ubuntu snapshot ${snapshot} after ${attempt} attempt(s)" >&2
  return 1
}

missing=()
for pkg in "$@" apparmor; do
  if ! [[ "$pkg" =~ ^[a-z0-9][a-z0-9+.-]+(:[a-z0-9][a-z0-9-]*)?$ ]]; then
    printf '::error::Native CI setup expects a Debian package name, got %q\n' "$pkg" >&2
    exit 1
  fi
  status="$(dpkg-query -W -f='${db:Status-Status}' -- "$pkg" 2>/dev/null)" || status=
  if [ "$status" != installed ]; then
    missing+=("$pkg")
  fi
done

# The write boundary uses --overlay-src (bubblewrap 0.10 and later), which the
# Ubuntu 24.04 package lacks. Use the checksum-pinned build from the same
# snapshot and let APT resolve its dependencies.
has_overlay_src() {
  local help
  help="$(bwrap --help 2>&1 || true)"
  case "$help" in *--overlay-src*) return 0 ;; *) return 1 ;; esac
}
bwrap_ready=false
if has_overlay_src; then
  bwrap_ready=true
fi

if [ "${#missing[@]}" -gt 0 ] || ! "$bwrap_ready"; then
  code_root="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
  pins="$code_root/.github/workflows/pins.txt"
  case "$code_root" in
    */lib/python*/site-packages|*/lib64/python*/site-packages)
      pins="${code_root%/*/*/*}/share/lu-machinery/ci/pins.txt"
      ;;
  esac
  if ! snapshot="$(awk -F= '
    $1 == "CI_UBUNTU_SNAPSHOT" { count++; value = substr($0, index($0, "=") + 1) }
    END { if (count != 1) exit 1; print value }
  ' "$pins")" || ! [[ "$snapshot" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
    echo "::error::Expected one CI_UBUNTU_SNAPSHOT in UTC (YYYYMMDDTHHMMSSZ) in ${pins}" >&2
    exit 1
  fi
  snapshot_url="https://snapshot.ubuntu.com/ubuntu/${snapshot}"

  # Keep the runner's sources, lists and archives untouched. Each invocation
  # starts with empty indexes, so a failed update cannot reuse stale metadata.
  # GitHub's RUNNER_TEMP can be private to the runner user. Use /tmp so
  # APT can read and download as its unprivileged _apt user.
  apt_dir="$(mktemp -d /tmp/lu-ci-apt-snapshot.XXXXXXXX)"
  trap 'sudo rm -rf -- "$apt_dir"' EXIT
  chmod 0755 "$apt_dir"
  mkdir -- "$apt_dir/lists" "$apt_dir/archives"
  # All CI native packages (bubblewrap deps, apparmor, postgresql-16, libpq-dev) live in main.
  cat > "$apt_dir/ubuntu.sources" <<EOF
Types: deb
URIs: ${snapshot_url}/
Suites: noble noble-updates noble-security
Components: main
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
Snapshot: no
EOF
  apt_opts=(
    -o "Dir::Etc::sourcelist=$apt_dir/ubuntu.sources"
    -o Dir::Etc::sourceparts=/dev/null
    -o "Dir::State::Lists=$apt_dir/lists"
    -o "Dir::Cache::Archives=$apt_dir/archives"
    -o Dir::Cache::pkgcache=
    -o Dir::Cache::srcpkgcache=
    -o Acquire::Languages=none
    -o Acquire::IndexTargets::deb::CNF::DefaultEnabled=false
    -o Acquire::Retries=3
    -o Acquire::https::Timeout=10
  )

  offline_installed=false
  if ! "$bwrap_ready"; then
    deb=bubblewrap_0.11.1-1ubuntu0.3_amd64.deb
    digest=d3a6c1b6b0e0474eaed6dbd055c0d601fd875af153976a0dc4aae75f4ea10868
    fetch_verified_bubblewrap_deb "$deb" "$digest" "$snapshot" "$snapshot_url" "$apt_dir"
    if [ "${#missing[@]}" -eq 0 ]; then
      echo "Installing verified ${deb} offline (all other requested packages are already installed)"
      offline_log="$apt_dir/offline-install.log"
      offline_rc=0
      offline_apt_install "$apt_dir/$deb" "$offline_log" 25 || offline_rc=$?
      if [ "$offline_rc" -ne 0 ] && grep -Fq 'E: dpkg was interrupted' "$offline_log"; then
        echo 'Repairing the interrupted dpkg transaction before retrying offline bubblewrap install'
        dpkg_with_lock_wait 15 --configure -a
        offline_rc=0
        offline_apt_install "$apt_dir/$deb" "$offline_log" 20 || offline_rc=$?
      fi
      if [ "$offline_rc" -eq 0 ]; then
        offline_installed=true
      else
        echo "Offline install of ${deb} exited ${offline_rc}; falling back to Ubuntu snapshot ${snapshot} for dependency resolution" >&2
      fi
    fi
    if ! "$offline_installed"; then
      missing+=("$apt_dir/$deb")
    fi
  fi

  if [ "${#missing[@]}" -gt 0 ]; then
    echo "Installing missing native packages from Ubuntu snapshot ${snapshot}: ${missing[*]}"
    apt_deadline=$((SECONDS + 195))
    # Reserve 55 s for the first apt install and 5 s kill grace per command.
    update_cap=$((apt_deadline - SECONDS - 65))
    if [ "$update_cap" -lt 1 ] ||
      ! sudo timeout --kill-after=5 "$update_cap" env -u APT_CONFIG apt-get "${apt_opts[@]}" update --error-on=any; then
      echo "::error::Could not update package indexes from Ubuntu snapshot ${snapshot}" >&2
      exit 1
    fi
    install_log="$apt_dir/install.log"
    install_rc=0
    apt_install 55 40 "${missing[@]}" || install_rc=$?
    if [ "$install_rc" -ne 0 ] && ! apt_reports_interrupted_dpkg &&
      apt_failure_is_dpkg_lock "$install_rc" "$install_log"; then
      retry_cap=$((apt_deadline - SECONDS - 5))
      [ "$retry_cap" -gt 15 ] || exit "$install_rc"
      [ "$retry_cap" -le 115 ] || retry_cap=115
      echo 'Retrying apt install after dpkg lock contention'
      install_rc=0
      apt_install "$retry_cap" "$((retry_cap - 15))" "${missing[@]}" || install_rc=$?
    fi
    if [ "$install_rc" -ne 0 ]; then
      apt_reports_interrupted_dpkg || exit "$install_rc"
      # Reserve 15 s for configure and 5 s kill grace per remaining command.
      retry_cap=$((apt_deadline - SECONDS - 25))
      if [ "$retry_cap" -lt 3 ]; then
        echo 'apt install ran out of time for interrupted dpkg recovery' >&2
        exit "$install_rc"
      fi
      [ "$retry_cap" -le 70 ] || retry_cap=70
      echo 'Repairing the interrupted dpkg transaction before retrying apt install'
      dpkg_with_lock_wait 15 --configure -a
      apt_install "$retry_cap" "$((retry_cap / 2))" "${missing[@]}"
    fi
  fi
else
  echo 'Native CI packages are already ready'
fi

if ! has_overlay_src; then
  echo "::error::bubblewrap still lacks --overlay-src after native package setup"
  exit 1
fi

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
bwrap --unshare-user --unshare-pid --ro-bind / / -- /usr/bin/true
