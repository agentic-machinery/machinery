#!/usr/bin/env bash
# Native test dependencies and the PostgreSQL 16 server for a CI pytest shard.
#
# Install missing native packages from the pinned Ubuntu snapshot and grant
# bwrap user namespaces. Start cluster 16/main and check that it serves
# LEARN_UKRAINIAN_CP_PG_DSN. Any failure exits non-zero. Tests that need their
# own clusters (initdb as the runner user) use the same binaries.
set -euo pipefail

: "${LEARN_UKRAINIAN_CP_PG_DSN:?LEARN_UKRAINIAN_CP_PG_DSN must be set}"
: "${RUNNER_TEMP:?RUNNER_TEMP must be set}"

bash -- "$(dirname -- "${BASH_SOURCE[0]}")/setup_bubblewrap.sh" postgresql-16 libpq-dev

if ! pg_lsclusters --no-header | grep -q '^16 main '; then
  sudo pg_createcluster 16 main
fi
# Start errors are not suppressed; only an already-online cluster is skipped.
if ! pg_lsclusters --no-header | awk '$1 == "16" && $2 == "main" && $4 == "online" {found = 1} END {exit !found}'; then
  sudo pg_ctlcluster 16 main start
fi
ready=""
for _ in $(seq 1 30); do
  if pg_isready -h localhost -p 5432 -q; then ready=1; break; fi
  sleep 2
done
if [ -z "$ready" ]; then
  echo "::error::PostgreSQL 16 was not ready on localhost:5432 within 60 s" >&2
  pg_lsclusters >&2 || true
  sudo tail -n 40 /var/log/postgresql/postgresql-16-main.log >&2 || true
  exit 1
fi
# 16/main must own the DSN port (5432), not some other server listening there.
cluster="$(pg_lsclusters --no-header | awk '$1 == "16" && $2 == "main" {print $3 " " $4}')"
if [ "$cluster" != "5432 online" ]; then
  echo "::error::PostgreSQL cluster 16/main is '${cluster:-missing}', expected '5432 online'" >&2
  pg_lsclusters >&2
  exit 1
fi
sudo -u postgres psql -p 5432 -v ON_ERROR_STOP=1 -q <<'SQL'
ALTER ROLE postgres WITH PASSWORD 'postgres';
SELECT 'CREATE DATABASE lu' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'lu')\gexec
SQL
# The exact DSN the tests use must connect, report major version 16, and
# serve the 16/main data directory.
version_num="$(PGPASSWORD=postgres psql "$LEARN_UKRAINIAN_CP_PG_DSN" -v ON_ERROR_STOP=1 -qtAc 'SHOW server_version_num')"
echo "PostgreSQL server_version_num=${version_num}"
if [ "${version_num:0:2}" != "16" ]; then
  echo "::error::DSN server reports version ${version_num}, expected major 16" >&2
  exit 1
fi
expected_dir="$(pg_lsclusters --no-header | awk '$1 == "16" && $2 == "main" {print $6}')"
dsn_dir="$(PGPASSWORD=postgres psql "$LEARN_UKRAINIAN_CP_PG_DSN" -v ON_ERROR_STOP=1 -qtAc 'SHOW data_directory')"
echo "DSN data_directory=${dsn_dir} (16/main: ${expected_dir})"
if [ -z "$expected_dir" ] || [ "$dsn_dir" != "$expected_dir" ]; then
  echo "::error::DSN server data_directory '${dsn_dir}' is not the 16/main data directory '${expected_dir:-missing}'" >&2
  pg_lsclusters >&2
  exit 1
fi

# Temporary PR-only acceptance probe for machinery #1144 row 11.
if [[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted ]]; then
  setup="$(dirname -- "${BASH_SOURCE[0]}")/setup_bubblewrap.sh"
  canary="$(mktemp -d -- "$RUNNER_TEMP/snapshot-canary.XXXXXXXX")"
  chmod 0755 "$canary"
  mkdir -p -- "$canary/present" "$canary/absent" "$canary/unavailable" \
    "$canary/checkout/.github/workflows" "$canary/checkout/lu_machinery/ci"

  sha256sum "$setup" .github/workflows/pins.txt
  RUNNER_TEMP="$canary/present" bash -- "$setup" postgresql-16 libpq-dev | tee "$canary/present.log"
  grep -Fxq 'Native CI packages are already ready' "$canary/present.log"

  sudo apt-get --yes --no-download remove -- postgresql-16 libpq-dev
  for extra in postgresql-16 libpq-dev; do
    status="$(dpkg-query -W -f='${db:Status-Status}' -- "$extra" 2>/dev/null)" || status=
    [[ "$status" != installed ]]
  done

  bad_pin=20230302T030400Z
  cp -- "$setup" "$canary/checkout/lu_machinery/ci/setup_bubblewrap.sh"
  sed "s/^CI_UBUNTU_SNAPSHOT=.*/CI_UBUNTU_SNAPSHOT=$bad_pin/" \
    .github/workflows/pins.txt > "$canary/checkout/.github/workflows/pins.txt"
  if RUNNER_TEMP="$canary/unavailable" bash -- "$canary/checkout/lu_machinery/ci/setup_bubblewrap.sh" \
    postgresql-16 libpq-dev > "$canary/unavailable.log" 2>&1; then
    echo "::error::The unavailable snapshot case succeeded" >&2
    exit 1
  fi
  cat "$canary/unavailable.log"
  grep -Fq "Could not update package indexes from Ubuntu snapshot $bad_pin" "$canary/unavailable.log"
  for extra in postgresql-16 libpq-dev; do
    status="$(dpkg-query -W -f='${db:Status-Status}' -- "$extra" 2>/dev/null)" || status=
    [[ "$status" != installed ]]
  done

  RUNNER_TEMP="$canary/absent" bash -- "$setup" postgresql-16 libpq-dev
  for extra in postgresql-16 libpq-dev; do
    [[ "$(dpkg-query -W -f='${db:Status-Status}' -- "$extra")" == installed ]]
  done
  if ! pg_isready -h localhost -p 5432 -q; then
    sudo pg_ctlcluster 16 main start
  fi
  PGPASSWORD=postgres psql "$LEARN_UKRAINIAN_CP_PG_DSN" -v ON_ERROR_STOP=1 -qtAc 'SELECT 1'

  echo 'SNAPSHOT-CANARY PASS: preinstalled packages skipped APT, absent packages installed, unavailable snapshot refused'
fi
