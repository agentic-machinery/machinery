# Clean-machine release drill

This kit checks that a published lu-machinery release works by itself on a machine that has never
seen a machinery checkout. It installs each release from that release's consumer lock into a fresh
virtual environment, runs every consumer row against that install, rolls back to the previous
release, runs every row again, and writes one JSON receipt per row per phase. It selects the same
`api`, `postgres`, and `pytest` extras as the deployed consumer. The Monitor API needs Uvicorn, Fleet plane status needs
psycopg, and the data-tier probe runs pytest.

Rows that need things only a production host has run against mocks instead: a synthetic product
checkout, small scrubbed extracts of real stores (`fixtures/`), fake credentials, stub command-line
tools (`gh`, `systemctl`, `tmux`, `sudo`, provider CLIs) and two loopback stand-ins (Monitor on
127.0.0.1:8765, Sources on 127.0.0.1:8766). `rows.json` lists every row with its class and mocks.
No real secret and no private data is used anywhere.

## What you need

- Linux with CPython 3.14 or newer for the release venv (`python3 --version`, or
  `--python "$(uv python find 3.14)"`). The drill script itself runs on system CPython 3.12+
  (such as Ubuntu 24.04's 3.12 or Debian 13's 3.13) when `--python` selects CPython 3.14+;
  if `python3` is older than 3.14, install 3.14 (`uv python install 3.14`) and pass
  `--python "$(uv python find 3.14)"`.
- `git` and `bash`.
- `strace`. Each row runs under it, and any machinery code loaded from outside the installed
  release fails the row. Install `strace` on the kit's fixed system `PATH` (`/usr/local/bin:/usr/bin:/bin`).
  `--no-strace` skips this check, and the receipts then say the origin
  was not checked.
- `bwrap` (bubblewrap), recommended. Each row then runs in a sandbox that binds nothing of the
  host except the system directories (`/usr`, `/etc` and the `/bin`, `/lib` links) read-only, the
  kit and the Python installation read-only, and the work directory read-write. `/tmp`,
  `/var/tmp` and `/run` are empty, the network namespace has only loopback, and home directories,
  host sockets and other checkouts are not visible. Without bwrap, ports 8765 and 8766 must be
  free and the kit must not sit inside a machinery checkout.
- Network access to github.com (release downloads) and pypi.org (dependencies) during setup only.
  pip ignores every pip configuration file on the machine and uses only pypi.org.
- About 1 GB of free disk space while it runs, under 1 GB of memory, and 10 to 15 minutes.

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

3. Run the whole drill. With no `--workdir` it creates a new `./lu-drill-<UTC time>` directory and
   touches nothing outside it (pip's cache and every row's home directory live inside it too):

   ```sh
   python3 -I drill.py all --version <release> --rollback-to <previous-release> \
     --python "$(uv python find 3.14)"
   ```

   Omit `--python` when `python3` is already CPython 3.14 or newer. Replace the placeholders with
   version numbers, without angle brackets.

   The kit downloads both wheels and `SHA256SUMS` from the GitHub release and checks the sums.
   It installs each release from that release's consumer lock: the `consumer.lock` text in the
   release notes, checked so its one `lu-machinery` pin is the wheel just verified, then
   `pip install --require-hashes` on that lock. The wheel comes from disk. The lock's other
   pins come from PyPI. It checks every installed file against the wheel's RECORD, runs all
   rows, rolls back to the previous release (RECORD check again), runs all rows again, then
   rolls forward and checks RECORD once more. An offline `--wheel-dir` must include
   `consumer.lock` next to that release's wheel and `SHA256SUMS`. At the end it deletes the
   venv, the mock world, the wheels and the traces of rows that passed, and keeps `out/`
   (pass `--keep-work` to keep everything).

4. Read the result. The kit prints `out/summary.txt` last: one screen of text, always under
   16 KiB, with the per-phase counts, each failed row with its reason, the RECORD checks, the
   origin check and the environment. The exit code is 0 when every row passed in both phases and
   every RECORD check passed, 1 when anything failed, and 2 when setup failed.

5. Reply with the text of `out/summary.txt`. Every detail stays on disk in `out/summary.json` and
   `out/receipts/<phase>/<row>.json`; receipts hold paths inside the work directory only.

To re-run some rows, add `--only R005` (a row id) or `--only api` (a probe id), with
`--workdir <earlier dir> --keep-work --reuse-venv` to keep the installed venv. A run with `--only` or `--forward-only` is reported as
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

## Fixtures: scrubbed extracts of real stores

`fixtures/` is built on a host that has the product checkout, never on the tester's machine, by
`extract_fixtures.py` (run it with `bash -c 'ulimit -v 2000000; python3 -I extract_fixtures.py
--product-root <product checkout>'`). It opens every store read-only and writes nothing back:

| File | Source | Kept | Dropped |
|---|---|---|---|
| `vesum-sample.tsv` | VESUM (`forms_all`, read through the lemma index, `LIMIT` per lemma) | word form, lemma, part of speech and tags for ten common words | row and entry ids, source comments and locations |
| `sources-tools.json` | the Sources MCP server's `server.py`, parsed with `ast`, never executed | tool names and whether each is read-only | everything else in the file |
| `task-records.json` | three terminal dispatcher records (schema 2) | status, mode, agent and model | ids, prompts, paths, branches, accounts, PIDs, hosts, timestamps and every other field |

The drill gives the task records synthetic ids, paths and timestamps. The extractor refuses output
that names a home directory, an e-mail address or a non-loopback IPv4 address. Credentials are
always fake. Session transcripts are never extracted: the digest rows use a synthetic session.

## What a pass does not prove

The kit proves the release installs, runs these consumer surfaces against mocks, and rolls back
and forward cleanly, with no machinery code from outside the release. It does not run a production
host's private wrappers, units or data, and it does not prove anything about a host's real
services; mocked answers stand in for them.
