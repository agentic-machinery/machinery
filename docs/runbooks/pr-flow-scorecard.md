# Collect PR size and first review wait

Call `collect_pr_flow_scorecard` once per repository. Supply the GitHub
`owner/name`, a local Git checkout with the PR head and base objects, and the
configured account that publishes recorded verdicts. Use Git 2.43 or later for
`git check-attr --source`. Fetch any missing PR objects before collecting size.
The collector reads a temporary Git object store so local settings and
uncommitted attributes cannot change the line counts.

```bash
.venv/bin/python - "$repo_slug" "$git_checkout" "$verdict_publisher" <<'PY'
import json
import sys
from pathlib import Path

from lu_machinery.fleet_comms.github_pr_metrics import collect_pr_flow_scorecard

repo, checkout, publisher = sys.argv[1:]
result = collect_pr_flow_scorecard(
    repo=repo, git_repo=Path(checkout), verdict_publisher=publisher,
)
print(json.dumps(result, indent=2))
PY
```

Pass `as_of` as a timezone-aware `datetime` to reproduce the cohort at a
specified cutoff. Keep the returned `cohort` with the gauges so you can verify
the PR numbers and the denominator.

## Verify the cohort

The collector traverses the repository's GitHub GraphQL merged PR connection.
The final page must end pagination, and the number of unique PRs must match
GitHub's final total. The collector filters by the cutoff and orders by
`mergedAt`, using the higher PR number first for ties. It takes 50 PRs or the
complete shorter history. If the source fails to prove completeness, both
gauges are absent and `cohort.reason` records the cause.

For an independent comparison, paginate [GitHub search](https://docs.github.com/en/search-github/searching-on-github/searching-issues-and-pull-requests#search-by-when-a-pull-request-was-merged) over a merge-time
window that contains the whole cohort. Reconcile the unique result count with
`total_count` and check `incomplete_results` on every page. Apply the same cutoff
and tie rule, then compare the PR numbers with `cohort.prs`.

## Read the measures

- `pr_size_lines` sums additions and removals in the merge-base diff between the
  forge's base and head SHAs. It uses `pr_size_warning.measure` to count renames
  at the destination and exclude test lines, fixtures, whole-file deletions, and
  paths marked `linguist-generated` in the committed head. An eligible binary
  file or missing Git object makes the size source unavailable.
- `first_exact_head_review_wait_seconds` measures time from PR creation to the
  first qualifying comment within the PR's creation-to-merge interval. It
  requires an unedited official `cf-verdict` publication from the configured
  publisher with a trusted repository association. The recorder checks the
  reviewed SHA against the live head before publication, so that publication
  still supplies evidence when the PR receives a later push. PRs without a
  qualifying recorded verdict contribute no review-wait sample.

Each measure reports `source_ok` and the sample count `n`. A healthy source with
samples emits the median and nearest-rank p90. When a source is unavailable,
the collector omits its `gauge` and gives a reason. A complete empty sample also
omits the gauge. The output includes repository and PR metadata only.

DevOps owns private scorecard wiring and the separate source decisions for the
revert/fix proxy and post-verdict pushes. See [issue #1141](https://github.com/learn-ukrainian/machinery/issues/1141).
