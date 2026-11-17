# Clean-machine release drill

This kit checks that a published lu-machinery release works by itself on a machine that has never
seen a machinery checkout. It installs the release from GitHub into a fresh virtual environment,
runs every consumer row against that install, rolls back to the previous release, runs every row
again, and writes one JSON receipt per row per phase.

Rows that need things only a production host has run against mocks instead: a synthetic product
checkout, synthetic stores, fake credentials, stub command-line tools (`gh`, `systemctl`, `tmux`,
`sudo`, provider CLIs) and two loopback stand-ins (Monitor on 127.0.0.1:8765, Sources on
127.0.0.1:8766). `rows.json` lists every row with its class and mocks.

## What you need

- Linux with CPython 3.12 or newer (`python3 --version`).
- `git` and `bash`.
- `strace`. Each row runs under it, and any machinery code loaded from outside the installed
  release fails the row. `--no-strace` skips this check, and the receipts then say the origin
  was not checked.
- `bwrap` (bubblewrap), recommended. It runs the rows in a private network namespace with empty
  `/home`, `/tmp`, `/var/tmp`, `/opt`, `/srv`, `/mnt` and `/media`, so a row can reach no network
  and no other checkout on the machine. Without it, ports 8765 and 8766 must be free and the kit
  must not sit inside a machinery checkout.
- Network access to github.com (release downloads) and pypi.org (dependencies) during setup only.
- About 1 GB of disk space and 10 minutes.

On Debian or Ubuntu: `sudo apt-get install -y python3 python3-venv git strace bubblewrap`.

## Steps

1. Start from a clean shell. The kit refuses to run if `PYTHONPATH` or any `LU_*` or
   `LEARN_UKRAINIAN_*` variable is set:

   ```sh
   env | grep -E '^(LU_|LEARN_UKRAINIAN_|PYTHONPATH=)' && echo "unset these first"
   ```

2. Get only this directory, not a machinery checkout:

   ```sh
   mkdir -p ~/drill-kit && cd ~/drill-kit
   curl -fsSL https://github.com/learn-ukrainian/machinery/archive/refs/heads/main.tar.gz \
     | tar -xz --strip-components=3 machinery-main/drill/clean-machine
   ls   # drill.py  probes.py  rows.json  README.md
   ```

3. Run the whole drill in a new, empty work directory:

   ```sh
   python3 -I drill.py all --workdir ~/drill-work --version 0.1.28 --rollback-to 0.1.27
   ```

   The kit downloads both wheels and `SHA256SUMS` from the GitHub release, checks the sums,
   installs v0.1.28, checks every installed file against the wheel's RECORD, runs all rows, rolls
   back to v0.1.27 (RECORD check again), runs all rows again, then rolls forward to v0.1.28 and
   checks RECORD once more.

4. Read the result. The last lines printed are the summary. The exit code is 0 when every row
   passed in both phases and every RECORD check passed, 1 when anything failed, and 2 when setup
   failed.

   ```sh
   cat ~/drill-work/out/summary.json
   ls ~/drill-work/out/receipts/forward ~/drill-work/out/receipts/rollback
   ```

5. Send back `out/summary.json` and the receipts of any failed row
   (`out/receipts/<phase>/<row>.json`). Receipts hold paths inside the work directory only.

To re-run some rows, add `--only R005` (a row id) or `--only api` (a probe id), with
`--reuse-venv` to keep the installed venv. A run with `--only` or `--forward-only` is reported as
`partial`, never as a pass.

## Receipts

Each receipt (`schema: lu-clean-machine-drill-receipt/1`) records the row id, kind, class, probe,
mocks, phase, release version, `pass` or `fail`, the reasons for a failure, the exit code, the UTC
start time, the duration in seconds, the origin check (method, violations, how many release code
files the row loaded) and the tail of the row's output.

`summary.json` (`schema: lu-clean-machine-drill-summary/1`) records per phase the row count, passes,
failures, failed row ids and total seconds; the class counts; the wheel hashes; the three RECORD
checks; and the environment (Python, platform, git, strace, isolation).

## Classes

| Class | Meaning |
|---|---|
| `as-is` | Needs only the installed release and a temp directory. |
| `mock` | Runs against the mocks listed in the row's `mocks`. |
| `cannot-run` | Cannot run off-host. Each such row would carry its reason. There are none. |

Rows whose real consumer does not use machinery at all run a stand-in that proves exactly that:
its interpreter cannot import the release, or it loads no release code. Rows that run as root on a
production host run through the `sudo` stand-in as the current user.

## What a pass does not prove

The kit proves the release installs, runs these consumer surfaces against mocks, and rolls back
and forward cleanly, with no machinery code from outside the release. It does not run a production
host's private wrappers, units or data, and it does not prove anything about a host's real
services; mocked answers stand in for them.
