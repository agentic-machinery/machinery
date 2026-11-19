#!/usr/bin/env bash
# Install bubblewrap and grant user namespaces on disposable CI VMs only.
# Optional package arguments share the apt transaction with the pinned bubblewrap build.
# Use image package lists first; switch to fallback mirrors only if that fails.
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
  sudo timeout --kill-after=5 "$cap" env LC_ALL=C apt-get "${apt_opts[@]}" \
    -o "DPkg::Lock::Timeout=$lock_wait" install -y --no-install-recommends -- "$@" \
    >"$install_log" 2>&1 || rc=$?
  cat "$install_log" || return "$?"
  if [ "$rc" -ne 0 ]; then
    echo "apt-get install failed (exit ${rc})" >&2
  fi
  return "$rc"
}

apt_reports_interrupted_dpkg() {
  grep -Fq 'E: dpkg was interrupted' "$install_log"
}

missing=()
for pkg in "$@" apparmor; do
  dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done

# The write boundary uses --overlay-src (bubblewrap 0.10 and later), which the
# Ubuntu 24.04 package lacks. Add the verified local build to the apt transaction
# instead of installing the older package first.
has_overlay_src() {
  local help
  help="$(bwrap --help 2>&1 || true)"
  case "$help" in *--overlay-src*) return 0 ;; *) return 1 ;; esac
}
if ! has_overlay_src; then
  deb=bubblewrap_0.11.1-1ubuntu0.3_amd64.deb
  digest=d3a6c1b6b0e0474eaed6dbd055c0d601fd875af153976a0dc4aae75f4ea10868
  fetched=
  for mirror in https://mirrors.edge.kernel.org/ubuntu https://archive.ubuntu.com/ubuntu \
    https://snapshot.ubuntu.com/ubuntu/20261113T000000Z; do
    if curl -fsSL --connect-timeout 4 --max-time 12 -o "${RUNNER_TEMP}/${deb}" \
      "${mirror}/pool/main/b/bubblewrap/${deb}"; then
      fetched=1
      break
    fi
  done
  if [ -z "$fetched" ]; then
    echo "::error::${deb} could not be fetched from any mirror"
    exit 1
  fi
  echo "${digest}  ${RUNNER_TEMP}/${deb}" | sha256sum -c -
  missing+=("${RUNNER_TEMP}/${deb}")
fi

if [ "${#missing[@]}" -gt 0 ]; then
  sudo rm -f /etc/apt/sources.list.d/*chrome* /etc/apt/sources.list.d/*google* \
    /var/lib/apt/lists/*chrome* /var/lib/apt/lists/*google*
  echo "Installing missing native packages: ${missing[*]}"
  apt_opts=(-o Acquire::Retries=1 -o Acquire::http::Timeout=10 -o Acquire::https::Timeout=10)
  # Keep 60 s for refreshing lists and 50 s for installing on fallback
  # mirrors. With 5 s kill grace per command, that path takes at most
  # 60 + 15 + 65 + 55 = 195 s. The pinned download adds at most 36 s.
  apt_deadline=$((SECONDS + 195))
  install_log="${RUNNER_TEMP}/bwrap-apt-install.log"
  install_rc=0
  apt_install 55 40 "${missing[@]}" || install_rc=$?
  if [ "$install_rc" -ne 0 ] && ! apt_reports_interrupted_dpkg &&
    apt_failure_is_dpkg_lock "$install_rc" "$install_log"; then
    echo 'Retrying apt install after dpkg lock contention'
    install_rc=0
    apt_install 115 100 "${missing[@]}" || install_rc=$?
    # Apt checks the journal only after it acquires the lock. A lock retry
    # can therefore be the first attempt to report an interrupted journal.
    if [ "$install_rc" -ne 0 ] && ! apt_reports_interrupted_dpkg; then
      exit "$install_rc"
    fi
  fi
  if [ "$install_rc" -ne 0 ]; then
    if apt_reports_interrupted_dpkg; then
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
    else
      # During the 2026-11-11 Canonical *.ubuntu.com DNS outage (status.canonical.com),
      # azure.archive.ubuntu.com, archive.ubuntu.com and security.ubuntu.com all failed to
      # resolve, and refreshing the default mirror list first blew test_env.sh's 240 s budget.
      # Switch immediately to Tier-1 HTTPS Ubuntu mirrors outside *.ubuntu.com (keeping
      # archive.ubuntu.com last); apt still verifies Signed-By ubuntu-archive-keyring.gpg.
      echo 'apt install failed against the image package lists; switching to fallback HTTPS Ubuntu mirrors'
      dpkg_with_lock_wait 10 --configure -a
      printf '%s\tpriority:%d\n' \
        'https://mirrors.edge.kernel.org/ubuntu/' 1 \
        'https://ftp.osuosl.org/pub/ubuntu/' 2 \
        'https://archive.ubuntu.com/ubuntu/' 3 | sudo tee /etc/apt/apt-mirrors.txt >/dev/null
      sudo chmod 0644 /etc/apt/apt-mirrors.txt
      if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
        sudo sed -i -E \
          's@^(URIs:[[:space:]]+).*(https?://([a-z0-9.-]+\.)?ubuntu\.com/ubuntu/?|mirror\+file:/etc/apt/apt-mirrors\.txt)([[:space:]]|$).*@\1mirror+file:/etc/apt/apt-mirrors.txt@' \
          /etc/apt/sources.list.d/ubuntu.sources
      fi
      if [ -f /etc/apt/sources.list ]; then
        sudo sed -i -E \
          '/^[[:space:]]*#/! s@https?://([a-z0-9.-]+\.)?ubuntu\.com/ubuntu/?([[:space:]]|$)@mirror+file:/etc/apt/apt-mirrors.txt\2@g' \
          /etc/apt/sources.list
      fi
      sudo timeout --kill-after=5 60 apt-get "${apt_opts[@]}" update -qq
      apt_install 50 30 "${missing[@]}"
    fi
  fi
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
