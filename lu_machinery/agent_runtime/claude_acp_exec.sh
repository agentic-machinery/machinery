#!/bin/sh
# ACP Claude seat launcher (#10500): the adapter reads this file from the running
# machinery package and checks ownership, modes and symlinks through /.
# It stores no digest pins and provides no runtime content-drift detection.
# Captured bytes run from a private copy named by CLAUDE_CODE_EXECUTABLE.
# This script sources no package files; any future dependency must join the
# package-relative closure. The host Claude Code identity is checked below.
# ACPX cannot pass a tool deny and --deny-all never covers
# tools that run without asking, so the headless background denies (#9690) are
# added here; the SDK's own --disallowedTools follows in "$@" and accumulates.
# Copy lease (R75): before anything else, hold a shared lock on this copy's
# lease on fd 9, which the exec below hands to Claude and every descendant that
# keeps it. The adapter removes a copy only under an exclusive lock, so it stays
# while any of them runs. The lease is opened read-only and never created: a
# missing lease means the copy is being removed, and the launch refuses. A
# remover may unlink the lease between the open and the lock, so the locked
# descriptor must still be the file at the lease path once the lock is held.
case $0 in /*/agent_runtime/claude_acp_exec.sh) ;; *) echo "claude_acp_exec: not run from a private launcher copy" >&2; exit 126 ;; esac
lease=${0%/agent_runtime/claude_acp_exec.sh}/lease.lock
{ command exec 9<"$lease"; } 2>/dev/null || { echo "claude_acp_exec: the launcher copy lease is missing" >&2; exit 126; }
/usr/bin/flock -s -n 9 || { echo "claude_acp_exec: the launcher copy is being removed, or this host lacks util-linux flock" >&2; exit 126; }
[ /dev/fd/9 -ef "$lease" ] || { echo "claude_acp_exec: the launcher copy was removed while this launch started" >&2; exit 126; }
unset lease
host=${LU_CLAUDE_ACP_HOST-}
identity=${LU_CLAUDE_ACP_HOST_IDENTITY-}
unset LU_CLAUDE_ACP_HOST LU_CLAUDE_ACP_HOST_IDENTITY
case $host in /*) ;; *) echo "claude_acp_exec: pinned Claude Code path is missing or relative" >&2; exit 126 ;; esac
case $identity in *:*:*:*:*) ;; *) echo "claude_acp_exec: pinned Claude Code identity is missing" >&2; exit 126 ;; esac
# Device, inode, size, and mtime and ctime in nanoseconds: an in-place edit after
# the version probe changes ctime. Only GNU stat prints exact nanoseconds; Ubuntu
# installs it as gnustat when stat is the uutils one, which rounds them.
stat=/usr/bin/gnustat
[ -x "$stat" ] || stat=/usr/bin/stat
[ "$("$stat" -c %d:%i:%s:%.9Y:%.9Z -- "$host" 2>/dev/null)" = "$identity" ] || { echo "claude_acp_exec: pinned Claude Code changed since its version probe, or this host lacks GNU stat" >&2; exit 126; }
# Claude Code starts in Manual whatever mode the adapter asks for: since
# claude-agent-acp 0.101.0 its default is Auto, which approves tools without an
# ACP permission request. The ACP seat only reads, so its built-in tools are an
# allowlist, which holds in every mode (an adapter can still switch a running
# session to Accept edits) and against tools a newer Claude Code adds: Read,
# Glob and Grep read the worktree; ToolSearch finds the sealed review MCP tool,
# and --tools leaves MCP tools as they are. The SDK sends --permission-mode=<mode>
# and --tools=<list> as one argument each (option values that start with "-" are
# joined the same way), so both are replaced. Their two-argument forms, which the
# SDK never sends, are refused, as is --dangerously-skip-permissions.
# The word list is expanded once, before the loop rebuilds "$@".
for arg in "$@"; do
    shift
    case $arg in
        --permission-mode=* | --tools=*) ;;
        --permission-mode | --tools | --dangerously-skip-permissions | --dangerously-skip-permissions=*)
            echo "claude_acp_exec: refusing the SDK argument ${arg%%=*}" >&2
            exit 126
            ;;
        *) set -- "$@" "$arg" ;;
    esac
done
exec "$host" --disallowedTools Monitor,ScheduleWakeup,CronCreate,Workflow \
    --tools=Read,Glob,Grep,ToolSearch --permission-mode=default "$@"
