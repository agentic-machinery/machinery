#!/bin/bash
# Hook: PreToolUse (Bash) — Enforce .venv/bin/python usage.
# Rejects bare `python3` or `python` commands with one copyable replacement.
# It never rewrites a command behind the caller's back.

# Read tool input from stdin
INPUT=$(cat)

# Extract the command from JSON. An unreadable payload or a missing jq must
# block, not pass every command through unchecked.
if ! COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty'); then
  printf 'enforce-venv: hook payload could not be read; blocking.\n' >&2
  exit 2
fi

if [ -z "$COMMAND" ]; then
  exit 0
fi

# Check if the command starts with an unqualified interpreter name.
# Match: "python3 ...", "python ...", but NOT ".venv/bin/python ..." or "/path/to/python ..."
if echo "$COMMAND" | grep -qE '^python3?[[:space:]]'; then
  # Already using venv? Skip.
  if echo "$COMMAND" | grep -qE '^\./\.venv/|^\.venv/|^/.*\.venv/'; then
    exit 0
  fi

  # A tool's working directory cannot choose a Python executable.
  # shellcheck source=agents_extensions/shared/hooks/context-rollover-lib.sh
  source "$(dirname "${BASH_SOURCE[0]}")/context-rollover-lib.sh" \
    || { printf 'enforce-venv: context-rollover-lib.sh is missing beside this hook\n' >&2; exit 2; }
  context_machinery_bootstrap || exit 2
  PYTHON_BIN="$CONTEXT_PROJECT_PYTHON"
  PYTHON_VERSION_FILE="$LU_PRIMARY_REPO_ROOT/.python-version"
  if [ -f "$PYTHON_VERSION_FILE" ]; then
    EXPECTED_PYTHON_VERSION=$(head -1 "$PYTHON_VERSION_FILE" | tr -d '[:space:]')
    ACTUAL_PYTHON_VERSION=$("$PYTHON_BIN" --version 2>&1)
    if [ -z "$EXPECTED_PYTHON_VERSION" ] \
      || [ "$ACTUAL_PYTHON_VERSION" != "Python $EXPECTED_PYTHON_VERSION" ]; then
      printf 'Unqualified interpreter blocked: expected Python %s, got %s\n' \
        "${EXPECTED_PYTHON_VERSION:-<missing pin>}" "${ACTUAL_PYTHON_VERSION:-<unavailable>}" >&2
      exit 2
    fi
  fi

  printf -v QUOTED_PYTHON '%q' "$PYTHON_BIN"
  case "$COMMAND" in
    python3*) FIXED="${QUOTED_PYTHON}${COMMAND#python3}" ;;
    python*) FIXED="${QUOTED_PYTHON}${COMMAND#python}" ;;
  esac

  printf 'Unqualified interpreter blocked. Run this command instead:\n  %s\n' "$FIXED" >&2
  exit 2
fi

# Qualified commands pass through unchanged.
exit 0
