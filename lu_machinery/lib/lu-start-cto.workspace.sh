#!/usr/bin/env bash
# lu-start-cto: start the combined CTO and cross-epic lead from the CTO workspace through the
# installed package launcher (start-claude-cto.sh: lease claim, SessionStart, drive-epic binding).
#
# Usage:  lu-start-cto [a|b] [--here]
#   a|b     Claude account for this start. Optional; it is also saved as the default, so
#           automatic relaunches use it too. Omitted: the saved default in CFG/claude-account.
#   --here  restart inside the current pane (a pane in session cto if that session exists);
#           without it a new tmux session "cto" is created, whose pane runs this start again
#           with --pane: --here that waits up to 30 s for the detached start to release the lock.
# Roots and the interpreter (LU_PRIMARY_REPO_ROOT, LU_STATE_ROOT, LEARN_UKRAINIAN_PYTHON, ...)
# come from the caller; the fleet repository selector comes only from SELECTOR_PROFILE.
# This is the only supported way to start the CTO seat from its workspace.
#
# Rendering (the released template; supervised rollover admits only its statements, pinned in
# lu_machinery.session_supervisor): replace each @-delimited name with one shell-quoted literal,
# no ~, $, `, or \. REPO is the CTO workspace's physical path; LAUNCHER is the installed package's
# start-claude-cto.sh by its physical path; SELECTOR_PROFILE is the deployed profile that sets
# LU_FLEET_REPO_SELECTOR_FILE; ACCOUNT_A and ACCOUNT_B are the two Claude account directories.
set -euo pipefail
REPO=@REPO@
CFG=@CFG@
LAUNCHER=@LAUNCHER@
SELECTOR_PROFILE=@SELECTOR_PROFILE@
MODEL=@MODEL@
EFFORT=@EFFORT@
KICK=@KICK@
ROLE=$CFG/CTO-ROLE-PROMPT.txt
ACCT_FILE=$CFG/claude-account
LOCK=$CFG/start.lock
# This start's own path, before any cd, for the pane that runs it again.
SELF="$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd -P)/${0##*/}"
# The selector, root and interpreter settings (lu_machinery.common.repo_root) a new pane takes
# from this caller, never from a reused tmux server's environment; an unset one is cleared.
PANE_ENV_VARS=(
  LU_FLEET_REPO_SELECTOR_FILE
  LU_PRIMARY_REPO_ROOT LEARN_UKRAINIAN_PRIMARY_REPO_ROOT
  LU_STATE_ROOT LEARN_UKRAINIAN_STATE_ROOT
  LU_PRODUCT_ROOT
  LU_PYTHON LEARN_UKRAINIAN_PYTHON
  LU_REPOS_ROOT LU_PROFILE_PATH
  LU_GITHUB_SLUG LEARN_UKRAINIAN_GITHUB_SLUG
)

HERE=0
PANE=0
ACCT=""
for arg in "$@"; do
  case "$arg" in
    --here) HERE=1 ;;
    --pane) HERE=1 PANE=1 ;;
    a|b) ACCT=$arg ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg (usage: lu-start-cto [a|b] [--here])" >&2; exit 2 ;;
  esac
done

# A supervised in-place successor is the predecessor's scoped launcher, which
# session_supervisor_exec_successor replaces with `lu-start-cto --here` after it stopped the
# provider: it exports SESSION_SUPERVISOR_WAKE_DELIVERY, and driver_scope exported
# LU_DRIVER_SCOPE_PID as that same process. Its own ancestors (the predecessor's scope waiter
# and, for a --here start, the flock holding start.lock) keep running, so it takes no new lock
# and its ancestors are not another CTO process.
SUCCESSOR=0
if [ "$HERE" = 1 ] && [ "$PANE" = 0 ] && [ -n "${SESSION_SUPERVISOR_WAKE_DELIVERY:-}" ] && [ "${LU_DRIVER_SCOPE_PID:-}" = "$$" ]; then
  SUCCESSOR=1
fi
# Every other start holds start.lock from before the checks through launch, in the flock parent
# only (-o keeps descendants from pinning it); a --here start holds it for its whole life. The
# start re-runs itself as the flock's child, which finds its parent holding this exact lock.
LOCK_ARGV=(flock -n -E 3 -o "$LOCK")
if [ "$PANE" = 1 ]; then
  LOCK_ARGV=(flock -w 30 -E 3 -o "$LOCK")
fi
if [ "$SUCCESSOR" = 0 ]; then
  HOLDER=()
  { mapfile -d '' -t HOLDER < "/proc/$PPID/cmdline"; } 2>/dev/null || HOLDER=()
  LOCKED=1
  for index in "${!LOCK_ARGV[@]}"; do
    [ "${HOLDER[index]-}" = "${LOCK_ARGV[index]}" ] || LOCKED=0
  done
  if [ "$LOCKED" = 0 ]; then
    exec "${LOCK_ARGV[@]}" "$0" "$@"
  fi
fi

[ -r "$ROLE" ] || { echo "missing $ROLE" >&2; exit 4; }
[[ "$SELECTOR_PROFILE" == /* && ! -L "$SELECTOR_PROFILE" && -f "$SELECTOR_PROFILE" && -r "$SELECTOR_PROFILE" ]] \
  || { echo "refused: missing or unreadable fleet repository selector profile $SELECTOR_PROFILE" >&2; exit 4; }
unset LU_FLEET_REPO_SELECTOR_FILE
# shellcheck source=/dev/null
. "$SELECTOR_PROFILE" || { echo "refused: failed to source $SELECTOR_PROFILE" >&2; exit 4; }
[[ -n "${LU_FLEET_REPO_SELECTOR_FILE:-}" && -n "${LU_FLEET_REPO_SELECTOR_FILE//[[:space:]]/}" ]] \
  || { echo "refused: $SELECTOR_PROFILE did not set LU_FLEET_REPO_SELECTOR_FILE" >&2; exit 4; }
export LU_FLEET_REPO_SELECTOR_FILE

ANCESTORS=" "
STAT_END=") "
if [ "$SUCCESSOR" = 1 ]; then
  PID=$PPID
  while [[ "$PID" =~ ^[0-9]+$ ]] && [ "$PID" -gt 1 ]; do
    ANCESTORS+="$PID "
    { STAT=$(< "/proc/$PID/stat"); } 2>/dev/null || break
    STAT=${STAT##*"$STAT_END"}
    STAT=${STAT#* }
    PID=${STAT%% *}
  done
fi
# The bracketed character keeps the check from matching its own command line.
RUNNING=0
PIDS=$(pgrep -f "[o]perator-appointed CTO AND cross-epic") || RUNNING=$?
case "$RUNNING" in
  0|1) ;;
  *) echo "refused: CTO process check failed" >&2; exit "$RUNNING" ;;
esac
for PID in $PIDS; do
  if [[ "$ANCESTORS" != *" $PID "* ]]; then
    echo "refused: a CTO process is still running" >&2
    exit 3
  fi
done
if tmux has-session -t =cto 2>/dev/null; then
  # The detached start reserves this session before it releases the lock.
  # Only a shell already in that session may restart it in place.
  if [[ "$HERE" = 0 || ! "${TMUX_PANE:-}" =~ ^%[0-9]+$ ]] ||
    [[ "$(tmux display-message -p -t "$TMUX_PANE" '#{session_name}')" != cto ]]; then
    echo "refused: tmux session cto exists (use --here inside it)" >&2
    exit 3
  fi
fi

if [ -z "$ACCT" ]; then
  ACCT=$(tr -d '[:space:]' < "$ACCT_FILE" 2>/dev/null || true)
  [ "$ACCT" = b ] || ACCT=a
fi
if [ "$ACCT" = b ]; then DIR=@ACCOUNT_B@; else DIR=@ACCOUNT_A@; fi
[ -s "$DIR/.credentials.json" ] || { echo "refused: account $ACCT ($DIR) is not logged in" >&2; exit 4; }
# Bounded, and read whole: grep -q under pipefail could fail a logged-in status on SIGPIPE.
AUTH=$(CLAUDE_CONFIG_DIR=$DIR timeout --foreground -k 2s 10s claude auth status </dev/null 2>/dev/null) || AUTH=""
[[ "$AUTH" == *'"loggedIn": true'* ]] \
  || { echo "refused: account $ACCT is not logged in; run: CLAUDE_CONFIG_DIR=$DIR claude, then /login" >&2; exit 4; }
printf '%s\n' "$ACCT" > "$ACCT_FILE"
echo "CTO seat: Claude account $ACCT ($DIR)"
export CLAUDE_CONFIG_DIR=$DIR
export CLAUDE_CODE_EXPERIMENTAL_CODE_MODE=1

LAUNCH=("$LAUNCHER" --epic epics --model "$MODEL" --effort "$EFFORT" "$KICK" -- --dangerously-skip-permissions)
cd -- "$REPO"
if [ "$HERE" = 1 ]; then
  ROLE_TEXT=$(cat "$ROLE")
  LC_WORKSPACE_ROOT="$REPO" exec "${LAUNCH[@]}" --append-system-prompt "$ROLE_TEXT"
fi
TMUX_ARGS=(new-session -d -s cto -e "CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR" -e "LC_WORKSPACE_ROOT=")
for var in "${PANE_ENV_VARS[@]}"; do
  TMUX_ARGS+=(-e "$var=${!var:-}")
done
# The pane runs this start again with this account and --pane, after clearing the settings
# this caller left unset. Its process, session and auth checks, its account save and its
# launch all run under the start.lock it takes, as any --here start's do, so whichever start
# takes the lock first runs on its own checks. Only that start reads the role text: a new
# tmux server can retain this client's argv, which must not look like a running CTO.
# shellcheck disable=SC2016
TMUX_ARGS+=(
  bash -c 'count=$1; shift; for v in "${@:1:count}"; do [ -n "${!v:-}" ] || unset "$v"; done; shift "$count"; unset LC_WORKSPACE_ROOT; "$@" || echo "lu-start-cto exited with status $?" >&2; exec bash'
  lu-start-cto "${#PANE_ENV_VARS[@]}" "${PANE_ENV_VARS[@]}" "$SELF" "$ACCT" --pane
)
# tmux reads an argument's trailing semicolon as a command separator.
for index in "${!TMUX_ARGS[@]}"; do
  if [[ "${TMUX_ARGS[index]}" == *';' ]]; then
    TMUX_ARGS[index]="${TMUX_ARGS[index]%;}\\;"
  fi
done
env -u LC_WORKSPACE_ROOT tmux "${TMUX_ARGS[@]}"
echo "started; attach with: tmux attach -t cto"
