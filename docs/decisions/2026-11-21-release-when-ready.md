# RELEASE-ASAP-1121

Date: 2026-11-21.

Owner order, 2026-11-21: release when a change is ready, not on a schedule.

## Decision

1. A machinery release is cut as soon as a ready change is on a green `main`.
   Ready means a change has merged (a post-release PR alone does not count),
   the previous release's post-release PR has merged too, and the newest
   `CI Gate` push run on `main`'s HEAD has passed with its CI evidence
   receipt. The complete list is the cut's preflight in `docs/release.md`
   (Cut a release, step 1).
2. There is no release schedule and no fixed window.
3. `claude-cto` runs every cut, so cuts never overlap. Workers and drivers
   report a merged change to `claude-cto` instead of running the cut.
4. The urgent-hotfix rule stays: an urgent fix is released at once. Other
   fixes are no longer held for a later window; they are released when ready
   too.
5. The rules stay: never rebuild or replace a published version, and never
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

## Residual

The release tool has no lock of its own. One runner is what keeps two cuts
from overlapping. A lock inside the tool is a separate follow-up owned by
`claude-cto`.

## Scope

`docs/release.md` carries the runbook. The release tool is not changed by
this record.
