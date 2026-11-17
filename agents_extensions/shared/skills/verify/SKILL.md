---
name: verify
description: Run verify_run for the api, sources, or host area before commit and before enqueue when a change touches that area.
---

# Verify

One command checks the area a change can break. Run it from a machinery checkout with the project interpreter. The JSON on stdout is the receipt. `--receipt PATH` writes the same document. Do not commit the receipt.

| Area | Run when the diff touches | Command |
| --- | --- | --- |
| api | `lu_machinery/api/`, Monitor route or page contracts, or dashboard HTML | `.venv/bin/python -m lu_machinery.ops.verify_run --area api` |
| sources | the Sources server, its unit, or a caller of `verify_word` | `.venv/bin/python -m lu_machinery.ops.verify_run --area sources` |
| host | `packaging/systemd/`, the loopback target, or a long-running unit | `.venv/bin/python -m lu_machinery.ops.verify_run --area host` |

Pass means exit 0 and `"ok": true` in the receipt.

- **api.** Every `all_page_contracts()` URL, every concrete HTTP route-contract path, and every parameter-free GET under those contracts. Each must answer in under 1 second with no 5xx.
- **sources.** The running Sources server's `/health` answers within 1 second, then one `verify_word` returns a VESUM match. This talks to the server the unit runs. It does not start a second process.
- **host.** Every unit in `Requires=` of `lu_machinery/packaging/systemd/learn-ukrainian-loopback.target` reports `active`.

Repeat `--area` to check more than one area in one receipt.
