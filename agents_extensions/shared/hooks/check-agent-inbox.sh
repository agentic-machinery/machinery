#!/bin/bash
# Hook: Check for unread cross-agent messages on every prompt submit.
# Serves every agent seat (claude/codex/gemini/agy/grok/orchestrator) via
# LEARN_UK_HOOK_RECIPIENT; the Gemini CLI seat is retired, the name is not.
# Uses the live inbox CLI so fleet-comms remains the authority for messages.
#
# Read only the Fleet inbox. Unknown storage remains visible to the agent.
#
# PIPELINE GUARD: Skips during build_module / ai_agent_bridge runs
# to prevent ping-pong between automated pipeline phases.

# Skip in pipeline/headless mode
if [ "${GEMINI_SESSION:-}" = "1" ] || [ "${LEARN_UKRAINIAN_PIPELINE:-}" = "1" ]; then
  exit 0
fi

HOOK_INPUT=$(cat)
# The installed hook-root rule selects the product, never this file's location.
# shellcheck source=agents_extensions/shared/hooks/context-rollover-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/context-rollover-lib.sh" \
  || { echo "check-agent-inbox.sh: context-rollover-lib.sh is missing beside this hook" >&2; exit 1; }
context_machinery_bootstrap "${LU_PRODUCT_ROOT:-${CLAUDE_PROJECT_DIR:-}}" || exit 1

RECIPIENT="${LEARN_UK_HOOK_RECIPIENT:-${SESSION_HANDOFF_AGENT:-claude}}"

RECIPIENT_LABEL=$(printf '%s' "$RECIPIENT" | tr '[:lower:]' '[:upper:]')
INBOX_PYTHON="$CONTEXT_PROJECT_PYTHON"

# This command reads only Fleet delivery counts and IDs. It never creates a
# database, claims mail or reads a message body. A missing store is unknown.
INBOX_OUTPUT=$(cd "$LEARN_UKRAINIAN_PRIMARY_REPO_ROOT" && "$INBOX_PYTHON" -I -m lu_machinery.fleet_comms deliveries pending --recipient "$RECIPIENT")
INBOX_STATUS=$?
if [ "$INBOX_STATUS" -ne 0 ] || ! printf '%s' "$INBOX_OUTPUT" | jq -e '.probe == "ok" and (.count | type == "number")' >/dev/null 2>&1; then
  CONTEXT="${RECIPIENT_LABEL} INBOX: unknown. Fleet delivery state could not be read.
cd $(printf '%q' "$LEARN_UKRAINIAN_PRIMARY_REPO_ROOT") && $(printf '%q' "$INBOX_PYTHON") -I -m lu_machinery.fleet_comms deliveries receive --recipient $(printf '%q' "$RECIPIENT") --worker-id $(printf '%q' "$RECIPIENT")"
  jq -n --arg msg "$CONTEXT" \
    '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":$msg}}'
  exit 0
fi
COUNT=$(printf '%s' "$INBOX_OUTPUT" | jq -r '.count')
if [ "$COUNT" -eq 0 ]; then
  exit 0
fi
PENDING_IDS=$(printf '%s' "$INBOX_OUTPUT" | jq -r '.ids[]')

# A UserPromptSubmit hook can fire repeatedly before another worker claims or
# acknowledges its message. Suppress only an identical recipient/session ID
# list. No session identity or unavailable state is deliberately fail-open.
SESSION_ID="${LEARN_UK_HOOK_SESSION_ID:-${LEARN_UKRAINIAN_SESSION_ID:-${CODEX_THREAD_ID:-${CODEX_SESSION_ID:-}}}}"
if [ -z "$SESSION_ID" ]; then
  SESSION_ID=$(printf '%s' "$HOOK_INPUT" | jq -r '.session_id // empty' 2>/dev/null)
fi
if [ -n "$SESSION_ID" ]; then
  STATE_DIR="$PROJECT_DIR/.agent/runtime"
  STATE_KEY=""
  if command -v shasum >/dev/null 2>&1; then
    STATE_KEY=$(printf '%s\n%s' "$RECIPIENT" "$SESSION_ID" | shasum -a 256 | awk '{print $1}')
  fi
  STATE_FILE="$STATE_DIR/inbox-${STATE_KEY}.ids"
  if [ -n "$STATE_KEY" ] && mkdir -p "$STATE_DIR" 2>/dev/null; then
    STATE_TMP=$(mktemp "$STATE_DIR/.inbox-${STATE_KEY}.XXXXXX" 2>/dev/null || true)
    if [ -n "$STATE_TMP" ]; then
      printf '%s\n' "$PENDING_IDS" > "$STATE_TMP"
      if [ -f "$STATE_FILE" ] && cmp -s "$STATE_TMP" "$STATE_FILE"; then
        rm -f "$STATE_TMP"
        exit 0
      fi
      if ! mv -f "$STATE_TMP" "$STATE_FILE" 2>/dev/null; then
        rm -f "$STATE_TMP"
      fi
    fi
  fi
fi

CONTEXT="${RECIPIENT_LABEL} INBOX: ${COUNT} unread message(s) waiting.
Message IDs:
${PENDING_IDS}
---
cd $(printf '%q' "$LEARN_UKRAINIAN_PRIMARY_REPO_ROOT") && $(printf '%q' "$INBOX_PYTHON") -I -m lu_machinery.fleet_comms deliveries receive --recipient $(printf '%q' "$RECIPIENT") --worker-id $(printf '%q' "$RECIPIENT")"

jq -n --arg msg "$CONTEXT" \
  '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":$msg}}'

exit 0
