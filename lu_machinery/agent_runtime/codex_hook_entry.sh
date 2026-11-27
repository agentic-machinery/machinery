#!/bin/bash
# Single, deterministic entry point for Codex lifecycle hooks.
#
# Codex launches every matching command hook for an event concurrently. Keeping
# the Bash policy checks in one runner avoids redundant interpreter startup,
# makes their order deterministic, and prevents policy hooks from contending
# with each other for Git/GitHub state.

set -u

MODE="${1:-}"
PAYLOAD=$(cat)

# SCRIPT_ROOT is the machinery tree holding this file: it supplies the code
# (the hooks and the policy module), never the project (machinery #100 R70).
SCRIPT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOOKS_DIR="$SCRIPT_ROOT/agents_extensions/shared/hooks"

# The payload's working directory stays policy input. The host binding supplies the roots.
# shellcheck source=lu_machinery/lib/project_interpreter.sh
source "$SCRIPT_ROOT/lu_machinery/lib/project_interpreter.sh" || {
  printf '%s\n' 'HOOK-BOOTSTRAP-FAILED: [hook_unavailable] installed interpreter library is missing.' >&2
  exit 2
}
project_host_context || exit 2
PYTHON_BIN="$LU_PYTHON"
CANONICAL_ROOT="$LU_PRIMARY_REPO_ROOT"

case "$MODE" in
  pre-tool-use)
    # Codex blocks only exit 2. The policy allows with exit 0 and no stdout;
    # every other status must fail closed. A sentinel preserves trailing
    # newlines so even newline-only unexpected stdout cannot look like allow.
    policy_out=$(
      printf '%s' "$PAYLOAD" \
        | project_python_run "$PYTHON_BIN" -m lu_machinery.agent_runtime.codex_hook_policy \
        --python-bin "$PYTHON_BIN" \
        --hooks-dir "$HOOKS_DIR" \
        --canonical-root "$CANONICAL_ROOT"
      policy_rc=${PIPESTATUS[1]}
      printf '.'
      exit "$policy_rc"
    )
    policy_rc=$?
    policy_out=${policy_out%.}
    case "$policy_rc" in
      0)
        [ -z "$policy_out" ] && exit 0
        printf '%s\n' 'CODEX-HOOK-POLICY-FAILED: the policy wrote unexpected stdout on allow; blocking the tool call fail-closed.' >&2
        exit 2
        ;;
      2)
        printf '%s' "$policy_out"
        exit 2
        ;;
      *)
        printf 'CODEX-HOOK-POLICY-FAILED: the policy exited %s (could not start, import or finish); blocking the tool call fail-closed.\n' "$policy_rc" >&2
        exit 2
        ;;
    esac
    ;;

  post-tool-use)
    printf '%s' "$PAYLOAD" \
      | CLAUDE_PROJECT_DIR="$CANONICAL_ROOT" bash "$HOOKS_DIR/tool-timing.sh"
    # stamp-pytest only inspects a Bash `command` (#8529); running it for
    # every other tool payload was pure interpreter-startup cost.
    TOOL_NAME=$(printf '%s' "$PAYLOAD" | jq -r '.tool_name // empty' 2>/dev/null || true)
    if [ "$TOOL_NAME" = "Bash" ]; then
      printf '%s' "$PAYLOAD" \
        | CLAUDE_PROJECT_DIR="$CANONICAL_ROOT" bash "$HOOKS_DIR/stamp-pytest.sh"
      # Primary-checkout dirt audit (#11253): exit 2 hands the attribution
      # report to the model; Codex runs PostToolUse for non-zero exits too.
      AUDIT_HOOK="$HOOKS_DIR/audit-primary-checkout-dirt.py"
      printf '%s' "$PAYLOAD" | "$PYTHON_BIN" -I "$AUDIT_HOOK"
      audit_rc=$?
      [ "$audit_rc" -eq 0 ] || exit "$audit_rc"
    fi
    ;;

  user-prompt-submit)
    printf '%s' "$PAYLOAD" \
      | CLAUDE_PROJECT_DIR="$CANONICAL_ROOT" \
        LEARN_UK_HOOK_RECIPIENT=codex \
        bash "$HOOKS_DIR/check-agent-inbox.sh"
    ;;

  *)
    printf 'Unknown Codex hook entry mode: %s\n' "$MODE" >&2
    exit 1
    ;;
esac
