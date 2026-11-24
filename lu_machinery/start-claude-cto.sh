#!/usr/bin/env bash
# The CTO seat's launcher from its workspace (DETACH step 4b): the Claude epics
# driver that loads the cto skill, run from a generated directory that is not a
# checkout (agent_deploy --workspace renders it) instead of the product checkout.
# The host start (lu-start-cto, workspace template) exports LC_WORKSPACE_ROOT,
# changes to that directory and runs this file from the installed release:
#   cd "$LC_WORKSPACE_ROOT" && "<site-packages>/lu_machinery/start-claude-cto.sh" ...
# Roots (common/repo_root.py, launcher_core.sh launcher_resolve_workspace_root):
# the workspace is the session root only. LU_PRIMARY_REPO_ROOT (the hook and
# interpreter trust anchor) is required; LU_STATE_ROOT and LU_PRODUCT_ROOT stay
# their own optional inputs. Without LC_WORKSPACE_ROOT this wrapper refuses: the
# product checkout keeps its own start-claude-cto.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/lu_machinery/lib/launcher_core.sh"

# Explicit CLI Opus escalation is limited to this wrapper's epics driver lane.
for arg in "$@"; do
  case "$arg" in
    --) break ;;
    --force) launcher_error "CTO launchers reject --force; run --help."; exit 2 ;;
    -h|--help)
      # The supported host start (lu-start-cto) always passes --model claude-opus-5-5;
      # without --model the cap guard keeps this wrapper on Sonnet (#12496).
      printf 'CTO defaults (these replace the generic driver defaults below): --epic epics --effort high;\n'
      printf '  --model claude-sonnet-5-5 unless --model is given. lu-start-cto passes --model claude-opus-5-5.\n'
      printf '  --force is refused. Runs only from the CTO workspace named by LC_WORKSPACE_ROOT.\n'
      ;;
  esac
done
# The lane is a fixed wrapper option: scope re-entry and successors replay the
# caller's argv, and this wrapper adds it exactly once.
LC_WRAPPER_LAUNCHER_ARGS=(--epic epics)
LAUNCHER_MODEL="${LAUNCHER_MODEL:-claude-sonnet-5-5}" \
LAUNCHER_EFFORT="${LAUNCHER_EFFORT:-high}" \
launcher_main claude driver "$@"
