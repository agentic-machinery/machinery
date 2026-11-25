"""Apply or verify release-owned agent extensions in an explicitly selected consumer."""
from __future__ import annotations

import argparse
import os
import sys

from .plan import Drift, Invalid, deploy, selected_tree


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy release fleet resources and consumer agent extensions.\n"
                    "Use in an enrolled Git checkout, or with --workspace in a generated non-Git workspace\n"
                    "(the CTO workspace); never targets the installation or home implicitly.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  .venv/bin/python -I -m lu_machinery.agent_deploy --project-root /work/project
  .venv/bin/python -I -m lu_machinery.agent_deploy --project-root /work/project --check
  .venv/bin/python -I -m lu_machinery.agent_deploy --project-root /work/project --check --print-manifest
  .venv/bin/python -I -m lu_machinery.agent_deploy --project-root /work/project --inputs-commit "$(git rev-parse HEAD)"
  "$LEARN_UKRAINIAN_PYTHON" -I -m lu_machinery.agent_deploy --workspace --project-root /srv/cto-workspace --check
Outputs: enabled provider files and .deploy-state/agent-deploy.v1.json; check writes nothing.
--print-manifest prints the ownership manifest the check recomposed and verified (JSON) instead of its summary.
--inputs-commit OID composes the plan (checked or applied) from that commit's descriptor and sources, never the
checkout's.
--workspace targets a directory that is neither a Git checkout nor inside one: its descriptor and sources are
read from that directory, as from a checkout, and must enable the claude provider (so its mandatory hook settings
are required). It needs an explicit --project-root (never LU_PRODUCT_ROOT, which names the product) and refuses
--inputs-commit (a workspace has no commits).
Exit codes: 0 verified/applied; 1 drift or application failure; 2 invalid/unsafe input.
Recovery after an interrupted fresh apply followed by source edits:
  Keep a copy of the desired source edits. Restore the product sources and installed
  release used by the interrupted apply, then apply again to establish ownership.
  Restore the desired source edits/release, apply again, and run --check.
  Do not delete provider directories or the ownership manifest: they may contain
  consumer files. If the interrupted inputs are unavailable, stop and reconcile
  differing unowned outputs individually against a trusted backup before retrying.
Related: agents_extensions/deployment.v1.json; machinery #100.""",
    )
    parser.add_argument("--project-root", default=None, help="Absolute consumer Git root, e.g. /work/project; default: LU_PRODUCT_ROOT, otherwise refuse")
    parser.add_argument("--check", action="store_true", help="Verify recomposed outputs without writing anything; default: apply")
    parser.add_argument("--print-manifest", action="store_true",
                        help="With --check: print the recomposed, verified ownership manifest on stdout; default: a summary")
    parser.add_argument("--workspace", action="store_true",
                        help="Target a generated non-Git workspace (outside every checkout); default: a Git checkout root")
    parser.add_argument("--inputs-commit", default=None, metavar="OID",
                        help="Read the descriptor and product sources from this full commit id; "
                             "default: the checkout")
    args = parser.parse_args()
    if args.print_manifest and not args.check:
        parser.error("--print-manifest requires --check")
    if args.workspace and args.project_root is None:
        parser.error("--workspace requires --project-root (LU_PRODUCT_ROOT names the product, not a workspace)")
    if args.workspace and args.inputs_commit is not None:
        parser.error("--workspace refuses --inputs-commit: a workspace has no commits")
    try:
        selected = args.project_root if args.project_root is not None else os.environ.get("LU_PRODUCT_ROOT")
        with selected_tree(selected, args.check, workspace=args.workspace) as tree:
            return deploy(tree, check=args.check, print_manifest=args.print_manifest,
                          inputs_commit=args.inputs_commit, workspace=args.workspace)
    except Invalid as exc:
        print(f"invalid deployment: {exc}", file=sys.stderr)
        return 2
    except Drift as exc:
        print(f"deployment drift: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"invalid or unsafe deployment input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
