#!/bin/bash
# Shared helpers for the shell hooks that run machinery code (#8511, #100 R51).
# Sourced, never registered as a hook; executing it directly does nothing.
# State I/O is delegated to the descriptor-relative Python helper (#9791).

# The physical path of directory $1, or return 1; as the installed rule's
# _project_physical_dir: the x sentinel keeps a trailing newline $(...) would
# strip, and a path with a newline refuses rather than being checked as its
# clean namesake.
_context_physical_dir() {
  local dir
  dir="$(cd -P "$1" 2>/dev/null && pwd -P && printf x)" || return 1
  dir="${dir%$'\n'x}"
  [[ "$dir" != *$'\n'* ]] || return 1
  printf '%s\n' "$dir"
}
# The deployment or launcher supplies the runtime. Only its isolated host
# context entry may select the product, state directory and machinery code.
context_machinery_bootstrap() {
  local python="${LU_PYTHON:-}" assignments
  if [[ "$python" != /* || ! -f "$python" || ! -x "$python" ]]; then
    printf '%s\n' 'HOOK-BOOTSTRAP-FAILED: [missing_interpreter_binding] LU_PYTHON must name an absolute executable.' >&2
    return 2
  fi
  assignments="$("$python" -I -B -m lu_machinery.host_context shell)" || return 2
  eval "$assignments"
  # shellcheck disable=SC2034  # Read by the hooks that source this library.
  PROJECT_DIR="$LU_PRODUCT_ROOT"
  MACHINERY_DIR="$LU_MACHINERY_CODE_ROOT"
  PROJECT_HOOK_STATE_ROOT="$LU_STATE_ROOT"
  CONTEXT_PROJECT_PYTHON="$LU_PYTHON"
  # shellcheck source=lu_machinery/lib/project_interpreter.sh
  source "$MACHINERY_DIR/lu_machinery/lib/project_interpreter.sh"
}

# The one project interpreter. Never fall back to PATH Python or a component override.
context_hook_state() {
  local operation="$1" project_dir="$2" session_id="$3" helper
  shift 3
  [ -n "${CONTEXT_PROJECT_PYTHON:-}" ] || context_machinery_bootstrap "$project_dir" 2>/dev/null || return 0
  # The helper is the imported release's copy, never the one beside this file. It is
  # stdlib-only: -I keeps the cwd and PYTHONPATH off its import path.
  helper="$MACHINERY_DIR/agents_extensions/shared/hooks/context-hook-state.py"
  [ -f "$helper" ] || return 0
  "$CONTEXT_PROJECT_PYTHON" -I "$helper" "$operation" "$PROJECT_HOOK_STATE_ROOT" "$session_id" "$@" 2>/dev/null || true
}

# Latest assistant input/cache usage from a Claude Code transcript, or 0.
# Output tokens are not current context usage and are deliberately excluded.
# An optional usage object lets statuslines apply the same rule to current_usage.
# Transcripts use the latest real model turn: Code Mode appends synthetic
# assistant entries (model "<synthetic>") whose usage is all zeros, so those are
# skipped; a real turn reporting zero is unknown usage, never an older turn's.
context_latest_usage_tokens() {
  local transcript="$1" usage="${2:-}"
  if [ -z "$usage" ]; then
    # Code Mode appends assistant rows whose model is "<synthetic>" and whose
    # usage is all zeros. Those rows are not the live turn. A real zero still
    # counts; no matching row is 0. The usage-argument branch below is unchanged.
    usage=$(tail -200 "$transcript" 2>/dev/null \
      | jq -s '[.[] | select(
          .type == "assistant"
          and (.message | type) == "object"
          and .message.model != "<synthetic>"
          and (.message.usage | type) == "object"
        )] | last | .message.usage // empty' 2>/dev/null)
  fi
  if [ -z "$usage" ] || [ "$usage" = "null" ]; then
    printf '0\n'
    return 0
  fi
  # The #10471 measurement found top-level usage summed both main passes around
  # an advisor call. Only the last message iteration describes current main context.
  printf '%s' "$usage" | jq -r '
    def tokens:
      (if type == "number" then .
       elif type == "string" then
         # Absolute anchors reject even a final newline: digits, optional fraction.
         if test("\\A[0-9]+(\\.[0-9]+)?\\z") then (tonumber? // 0) else 0 end
       else 0 end)
      | if isnan or (isfinite | not) or . > 1000000000000000 then 0
        else [., 0] | max | floor end;
    (if (.iterations | type) == "array" then
       ([.iterations[] | select(type == "object" and .type == "message")] | last) // .
     else . end)
    | ((.input_tokens | tokens) + (.cache_read_input_tokens | tokens) + (.cache_creation_input_tokens | tokens))
    | floor
  ' 2>/dev/null || printf '0\n'
}

# Live context usage for a session as "<tokens>\t<source>"; tokens 0 means
# unknown. Order: (1) the status line's harness figure from this session's
# record, when observed within the last 300 seconds; (2) the latest real model
# turn in the transcript (headless sessions have no status line); (3) unknown.
# Usage is never estimated from transcript size. A status-line zero (no usage
# yet, none since /compact, or malformed) is no figure and falls to (2).
# 300 seconds: the status line runs after every model response. It records a
# changed figure at once and an unchanged one at least once a minute, so the
# record always holds the figure of its latest run, stamped at most 60 seconds
# before that run. The stamp is a freshness bound, never an ordering key: a
# throttled stamp can only retire the latest figure early (falling to the
# transcript), never let an older figure stand in for a newer one. Past 300
# seconds the status line has not run for four minutes, and the transcript
# reports the same latest turn anyway.
CONTEXT_STATUSLINE_MAX_AGE_SECONDS=300
context_live_usage() {
  local record_file="$1" session_id="$2" transcript="$3" tokens=0 now
  if [ -f "$record_file" ] && [ ! -L "$record_file" ]; then
    printf -v now '%(%s)T' -1
    tokens=$(jq -r --arg sid "$session_id" --argjson now "$now" \
      --argjson max "$CONTEXT_STATUSLINE_MAX_AGE_SECONDS" '
      (.observed_context_used_tokens) as $used | (.observed_context_used_at_epoch) as $at
      | if .session_id == $sid and ($used | type) == "number" and ($at | type) == "number"
           and $used > 0 and ($now - $at) <= $max and ($at - $now) <= 60
        then $used | floor else 0 end' "$record_file" 2>/dev/null) || tokens=0
    case "$tokens" in ''|*[!0-9]*) tokens=0 ;; esac
    if [ "$tokens" -gt 0 ]; then
      printf '%s\tstatus line (harness live usage)\n' "$tokens"
      return 0
    fi
  fi
  if [ -n "$transcript" ] && [ -f "$transcript" ]; then
    tokens=$(context_latest_usage_tokens "$transcript")
    case "$tokens" in ''|*[!0-9]*) tokens=0 ;; esac
    if [ "$tokens" -gt 0 ]; then
      printf '%s\tlatest assistant input/cache usage\n' "$tokens"
      return 0
    fi
  fi
  printf '0\tunknown\n'
}

context_git_common_dir() {
  git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null
}

context_checkout_root() {
  local project_dir="$1" common_dir_reader="${2:-context_git_common_dir}" common_dir
  if [ -n "${CODEX_CANONICAL_REPO_ROOT:-}" ]; then
    printf '%s\n' "$CODEX_CANONICAL_REPO_ROOT"
    return 0
  fi
  common_dir=$("$common_dir_reader" "$project_dir" || true)
  if [ -n "$common_dir" ] && [ "$(basename "$common_dir")" = ".git" ]; then
    dirname "$common_dir"
  else
    printf '%s\n' "$project_dir"
  fi
}

# Durable hooks share the state directory from the host binding.
context_canonical_root() {
  if [ -z "${PROJECT_HOOK_STATE_ROOT:-}" ]; then
    printf '%s\n' 'HOOK-BOOTSTRAP-FAILED: [missing_state_binding] hook context was not loaded.' >&2
    return 1
  fi
  printf '%s\n' "$PROJECT_HOOK_STATE_ROOT"
}

# True when <dir> is an existing directory outside every checkout: no .git entry
# (directory, file or symlink) at its physical path or any ancestor, the rule the
# launcher and the deployer's workspace mode use. Decided from the filesystem only,
# so an inherited GIT_DIR or GIT_WORK_TREE cannot make a workspace look like one.
context_outside_checkouts() {
  local dir
  dir="$(_context_physical_dir "${1:-}")" || return 1
  while :; do
    [ ! -e "$dir/.git" ] && [ ! -L "$dir/.git" ] || return 1
    [ -n "$dir" ] && [ "$dir" != / ] || return 0
    dir="${dir%/*}"
  done
}

# LU_STATE_ROOT owns session records. Checkout-mode consumers of the legacy
# alias can still supply an explicit session record file.
context_session_record_file() {
  local project_dir="$1" session_id="$2"
  if [ -z "${LU_STATE_ROOT:-}" ] && [ -n "${LEARN_UKRAINIAN_SESSION_RECORD:-}" ] \
    && [ -f "$LEARN_UKRAINIAN_SESSION_RECORD" ]; then
    printf '%s\n' "$LEARN_UKRAINIAN_SESSION_RECORD"
    return 0
  fi
  local root
  root="$(context_canonical_root "$project_dir")" || return 1
  printf '%s\n' "$root/.agent/sessions/$session_id.json"
}

# Effective rollover mode for this session. operator_restart applies only when
# an operator can restart the session: a delegated worker has nobody to wait
# for, so it keeps the continuation behaviour and its native compaction.
context_effective_rollover_mode() {
  if [ "${1:-}" = "operator_restart" ] && [ -z "${LEARN_UKRAINIAN_DISPATCH_TASK_ID:-}" ]; then
    printf 'operator_restart\n'
  else
    printf 'continuation\n'
  fi
}

# The name of the guard-managed driver lane this session runs in, or nothing.
# scripts/ops/context_guard.py hands off and restarts the lanes in
# scripts/config/driver_lanes.json by tmux session name, so the operator is not
# the one who restarts such a session. Both facts must hold: the launcher's
# exported SESSION_EPIC names a lane epic, and this pane's tmux session is that
# lane's session. The registry is read with the guard's own loader, so a
# registry the guard rejects (duplicate session, malformed file) yields no lane.
# Anything missing or failing means "not guard-managed": the operator text stays.
context_driver_lane_name() {
  local project_dir="$1" interpreter session name
  local registry_module=lu_machinery.ops.driver_lanes
  [ -n "${SESSION_EPIC:-}" ] && [ -n "${TMUX:-}" ] && [ -n "${TMUX_PANE:-}" ] || return 0
  command -v tmux >/dev/null 2>&1 || return 0
  session=$(tmux display-message -p -t "$TMUX_PANE" '#{session_name}' 2>/dev/null) || return 0
  [ -n "$session" ] || return 0
  [ -n "${CONTEXT_PROJECT_PYTHON:-}" ] || context_machinery_bootstrap "$project_dir" 2>/dev/null || return 0
  interpreter="$CONTEXT_PROJECT_PYTHON"
  # The registry module runs from the code root through the shared module bootstrap.
  name=$(project_python_run "$interpreter" -c '
import importlib
import sys
load_registry = importlib.import_module(sys.argv[1]).load_registry
lanes = [
    lane.name
    for lane in load_registry().lanes
    if lane.session == sys.argv[2] and lane.epic == sys.argv[3] and lane.kind == "claude"
]
print(lanes[0] if len(lanes) == 1 else "")
' "$registry_module" "$session" "$SESSION_EPIC" 2>/dev/null) || return 0
  printf '%s\n' "$name"
}

# The driver-state lane whose DB note holds this launcher-bound session's
# handoff content (#511): cto for the CTO seat, otherwise SESSION_EPIC, the
# lane the launcher prompt names. Prints nothing outside a launcher-bound lane
# or for a malformed name, so a worker or an ordinary session keeps its own
# handoff flow.
context_handoff_note_lane() {
  local LC_ALL=C
  case "${SESSION_EPIC:-}" in
    ''|*[!a-z0-9.-]*|[-.]*|*[-.]) return 0 ;;
  esac
  if [ "${SESSION_SEAT:-}" = cto ]; then printf 'cto\n'; else printf '%s\n' "$SESSION_EPIC"; fi
}

# The one statement of handoff content authority for lane $1. Cold start,
# compaction and replacement context all print it, so every harness reads the
# same note: the lane fence of lu-driver-state render and the driver keeper.
# Prints nothing without a lane.
context_handoff_authority() {
  [ -n "${1:-}" ] || return 0
  printf '%s\n' "Handoff content: the lane's DB note, its newest driver-state handoff_note that is not done or dropped (lane $1).
Read it first: lu-driver-state render --lane $1, then lu-driver-state show <its ID> for the full note.
DRIVER-STATE.md is a pinned-facts pointer. Markdown handoffs, briefs and thread-rollover packets never outrank the DB note."
}
