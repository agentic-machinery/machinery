# Fleet Comms failure metrics

The authority-mode `metrics` CLI and `/api/comms/v1/metrics` expose
`failure_windows.24h` and `failure_windows.7d`. Each contains `dead_letters`,
`failed_deliveries` and `failed_jobs`, with a `total` and `by_recipient`.
`failure_lifetime` contains separate lifetime totals. The older `dead_letters`,
`deliveries` and `jobs` fields retain their lifetime meanings for compatibility.
The Comms panel and summary chip warn from recent values only. Missing recent
measurements display `?`; they are not inferred from lifetime counts.

Windows include both endpoints and exclude future events. Dead letters use
`created_at`; failed deliveries and jobs use `completed_at`, falling back to
`updated_at` for historical rows. An old job that fails today counts today.
The deliberate `deliveries expire-stale --apply` cleanup records its dead letters
with reason `expired_by_cleanup`; no window, lifetime failure total or alert
gauge counts them, and the fleet authority-health window ignores them too. A
genuine `deadline_expired` row, even in the same cleanup batch, still counts.
Only state `failed` counts in the two failure measures; expired/dead-lettered
rows remain represented by the separate dead-letter measure.

Each `dead_letters` window also carries `by_cause`, one count per cause:
`recipient_not_registered`, `recipient_settled`, `attempts_exhausted`,
`expired_unread` (reason `deadline_expired`), `worker_failed`, `job_expired` (a
job's deadline) and `other`. The causes sum to `total`. The CLI shows them now;
`/api/comms/v1/metrics` drops the key until its public filter passes it through
(the Fleet page work in #592).

A request job inherits its message's recipient only when there is exactly one.
Other jobs use `authority-job`, so fan-out never multiplies the event count.
Older PostgreSQL job schemas without subject metadata also use `authority-job`;
when they lack `completed_at`, the reader uses `updated_at`.
The CLI preserves exact recipients. Public JSON and Prometheus retain fixed
agent names, `claude-cto`, `kimi` and `authority-job`, combining other identities
in `other`. They do not expose private session identities or unbounded labels.
Legacy mode retains its older metrics; recent authority measurements are
unavailable there.

## Alert integration

The existing Monitor `/metrics` scrape collects independently of page visits:

- `lu_fleet_comms_dead_letters_recent{window="24h"}` and `{window="7d"}`
- `lu_fleet_comms_failed_deliveries_recent{window="24h"}` and `{window="7d"}`
- `lu_fleet_comms_failed_jobs_recent{window="24h"}` and `{window="7d"}`
- Each measure also has a `_recent_by_recipient{window,recipient}` gauge.
- `lu_fleet_comms_collection_success` is 1 on collection success and 0 when
  unavailable. Unavailable measurements are omitted rather than emitted as zero.

These are rolling gauges, not monotonic counters. Do not apply `rate()`.
Suggested initial alert: `lu_fleet_comms_dead_letters_recent{window="24h"} > 0`
for 5 minutes. Alert separately on collection success 0 or absent scrape data.
The private infrastructure repository owns alert rules and routing to the CTO;
this change supplies measurements and does not install those rules.

## Only readers get mail

The authority creates a delivery only for a recipient a drain reads: `claude-cto`,
a slot in `config/area_assignments.yaml`, a lane sender in
`config/driver_lanes.json`, a supervisory watcher (`supervisor:epic:<n>`) or a
live task id (task record status `spawning` or `running`). Publishing or
subscribing to anything else fails before any write with a typed error that
names the recipient: `recipient_not_registered: <name> ...` (bare provider
names, retired seats and ad-hoc worker names) or `recipient_settled: <task> ...`.
Request and discussion jobs are their own queue, so their messages create no
delivery.

When a task settles, the next delivery claim or reclaim expires its open
deliveries as `recipient_settled` dead letters, which keeps the rows, notifies
`claude-cto` of its unread messages and ends the task's channel subscriptions. A
channel broadcast also skips a subscriber that no drain reads and ends that
subscription.

## One-time hygiene of unread mail

Count first; this writes nothing:

```bash
.venv/bin/python -m lu_machinery.fleet_comms deliveries expire-stale
```

`unreadable` gives the open deliveries whose recipient no drain reads, by cause,
and the number of such subscriptions. `--apply` expires those rows as
`recipient_not_registered` or `recipient_settled` dead letters (rows and
messages stay, nothing is deleted), ends those subscriptions and sends one
summary notice per cause to `claude-cto`. It then applies the usual 7-day
cutoff to the rest. Queued rows to the retired Kimi seats are expired too, as
`recipient_not_registered` (a state change only; it is the one writer that does
this). These expiries count in the recent windows under their cause.

## Remove a stale subscription

```bash
.venv/bin/python -m lu_machinery.fleet_comms channel unsubscribe CHANNEL RECIPIENT \
  --idempotency-key UNIQUE-REMOVAL-KEY
```

The command removes future implicit fan-out only. Existing messages, deliveries,
explicit recipients and acknowledgments are unchanged. All subscribers must
exist or the transaction fails. Retired identities can be removed without
re-admitting them. An unknown channel also fails.

A durable `authority_idempotency` receipt records the operation's channel,
payload hash, key and timestamp atomically with removal. Reuse the key for a
retry. A retry after re-subscription leaves the new subscription intact; use a
new key to remove that new subscription. Reusing a key with a different channel
or recipient set fails.
