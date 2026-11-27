#!/usr/bin/env bash
# Shared path and runtime helpers for the tracked Git hook chain.

set -euo pipefail

hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ "${LU_PYTHON:-}" != /* || ! -f "$LU_PYTHON" || ! -x "$LU_PYTHON" ]]; then
  printf '%s\n' 'Git hook error [missing_interpreter_binding]: LU_PYTHON must name an absolute executable.' >&2
  exit 127
fi
# The isolated entry validates the binding before inspecting invocation metadata.
hook_context="$("$LU_PYTHON" -I -B -m lu_machinery.host_context shell --require-checkout)" || exit 127
worktree_root='' main_root=''
eval "$hook_context"
unset hook_context
if [[ -z "$worktree_root" || -z "$main_root" ]]; then
  printf '%s\n' 'Git hook error [missing_checkout_binding]: the hook entry returned no checkout.' >&2
  exit 127
fi

source_root="$worktree_root"
if [[ ! -f "$source_root/lu_machinery/lib/project_interpreter.sh" || ! -f "$source_root/.pre-commit-config.yaml" ]]; then
  source_root="$main_root"
fi

run_project_python() {
  "$LU_PYTHON" "$@"
}

run_pre_commit() {
  local hook_type="$1"
  shift
  run_project_python \
    -m pre_commit hook-impl \
    -c "$source_root/.pre-commit-config.yaml" \
    --hook-type "$hook_type" \
    --hook-dir "$hook_dir" \
    -- "$@"
}

# A dispatched write task learns at commit or push time that it changed a path outside its
# owned paths; settlement still refuses such a delivery (#12113). Work outside a task skips
# it. A dispatched task whose checker is not in the tree or the installed package is refused:
# a missing checker must not skip enforcement.
check_owned_paths() {
  if [[ -z "${LEARN_UKRAINIAN_DISPATCH_TASK_ID:-}" ]]; then
    return 0
  fi
  if [[ -f "$source_root/lu_machinery/dispatcher/ownership.py" ]]; then
    (cd "$source_root" && run_project_python -m lu_machinery.dispatcher.ownership --worktree "$worktree_root" "$@") || exit 1
    return 0
  fi
  if ! "$LU_PYTHON" -I -c 'import lu_machinery.dispatcher.ownership' >/dev/null 2>&1; then
    echo "Git hook error: owned-path checker is not installed; refusing to skip enforcement." >&2
    exit 1
  fi
  "$LU_PYTHON" -I -m lu_machinery.dispatcher.ownership --worktree "$worktree_root" "$@" || exit 1
}

require_git_lfs() {
  if ! command -v git-lfs >/dev/null 2>&1; then
    echo "Git hook error: this repository uses Git LFS, but git-lfs is not installed." >&2
    exit 2
  fi
}
