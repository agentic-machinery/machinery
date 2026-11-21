# RELEASE-ASAP-1121

Date: 2026-11-21.

Owner order, 2026-11-21: release when a change is ready, not on a schedule.

## Decision

1. A machinery release is cut as soon as a ready change is on a green `main`.
   Ready means a change has merged (a post-release PR alone does not count)
   and the newest `CI Gate` push run on `main`'s HEAD has passed.
2. The 06:00 and 18:00 UTC timer stays as a safety net. It skips when nothing
   new has merged since the last full release.
3. The urgent-hotfix rule stays: an urgent fix is released at once. Other fixes
   are no longer held for a later window; they are released when ready too.
4. The rules stay: never rebuild or replace a published version, and never
   move a tag.

## What it replaces

It replaces the release cadence in
[ONE-REVIEW-FAST-RELEASE-1112](2026-11-12-one-review-release-train.md)
(item 2: one release per day, later changed to every 12 hours by
RELEASE-8H-CANARY-AT-CUT-1115). The rest of that record stands: one review per
change and the merge queue grouping.

## Why

A fixed window made a finished change wait for hours with no gain. The cut
already checks everything that matters, and it skips when nothing changed, so
cutting earlier costs nothing extra.

## Scope

`docs/release.md` carries the runbook. The release tool, the host timer and
host units are not changed by this record; the host side is handled
separately by `claude-cto`.
