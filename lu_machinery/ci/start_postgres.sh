#!/usr/bin/env bash
# Set up bubblewrap and start the hosted image's PostgreSQL 18 for a CI shard.
set -euo pipefail

: "${LEARN_UKRAINIAN_CP_PG_DSN:?LEARN_UKRAINIAN_CP_PG_DSN must be set}"
: "${RUNNER_TEMP:?RUNNER_TEMP must be set}"

bash -- "$(dirname -- "${BASH_SOURCE[0]}")/setup_bubblewrap.sh"
sudo systemctl start postgresql.service

# 18/main must own the test port. Check it before changing the role or database.
cluster="$(pg_lsclusters --no-header | awk '$1 == "18" && $2 == "main" {print $3, $4, $6}')"
read -r port status expected_dir <<< "$cluster"
if [ "$port" != 5432 ] || [ "$status" != online ] || [ -z "$expected_dir" ]; then
  echo "::error::Expected the image's PostgreSQL 18/main cluster online on port 5432" >&2
  pg_lsclusters >&2
  exit 1
fi

sudo -u postgres psql -X -p 5432 -v ON_ERROR_STOP=1 -q <<'SQL'
ALTER ROLE postgres WITH PASSWORD 'postgres';
SELECT 'CREATE DATABASE lu' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'lu')\gexec
SQL

# Check the exact DSN and its server, not just whether the port accepts TCP.
version_num="$(psql "$LEARN_UKRAINIAN_CP_PG_DSN" -X -v ON_ERROR_STOP=1 -qtAc 'SHOW server_version_num')"
echo "PostgreSQL server_version_num=${version_num}"
if ! [[ "$version_num" =~ ^18[0-9]{4}$ ]]; then
  echo "::error::DSN server reports version ${version_num}, expected major 18" >&2
  exit 1
fi
dsn_dir="$(psql "$LEARN_UKRAINIAN_CP_PG_DSN" -X -v ON_ERROR_STOP=1 -qtAc 'SHOW data_directory')"
if [ "$dsn_dir" != "$expected_dir" ]; then
  echo "::error::DSN server data_directory '${dsn_dir}' differs from 18/main '${expected_dir}'" >&2
  exit 1
fi
echo "DSN connects to the image's PostgreSQL 18/main cluster"
