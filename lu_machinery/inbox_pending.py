"""Body-free, read-only pending-order probe for AGY PreInvocation (#10558)."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import TextIO

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import (
    HOOK_BOOTSTRAP_DIAGNOSTIC,
    HOOK_BOOTSTRAP_REASON,
    configure_native_hook_roots,
)
from lu_machinery.orchestration.handoff_slot_registry import is_registered_slot, registry_readable

# The machinery package's import name, which is also the directory its files have in checkout
# paths: from this module's package, or from its unresolved location when run as a file.
_MACHINERY = Path(os.path.abspath(__file__)).parents[0].name
_AUTHORITY_HELP = f".venv/bin/python -m {_MACHINERY}.fleet_comms deliveries --help"
UNKNOWN_LINE = (
    f"PENDING ORDERS: unknown; authority: {_AUTHORITY_HELP}"
)


def probe(recipient: str, *, limit: int = 5) -> dict:
    """Read only Fleet delivery metadata; the retired broker is historical."""
    from lu_machinery.fleet_comms.authority import pending_delivery_snapshot

    valid = (
        re.fullmatch(r"[a-z]+-[a-z0-9]+(?:-[a-z0-9]+)*", recipient)
        and registry_readable() and is_registered_slot(recipient)
    )
    result = pending_delivery_snapshot(recipient, limit=limit) if valid else {"probe": "unknown"}
    if result["probe"] != "ok":
        return {
            "count": 0, "ids": [], "truncated": False,
            "stores": {"authority": None}, "probe": "unknown",
        }
    return {
        "count": result["count"], "ids": result["ids"],
        "truncated": result["truncated"], "stores": {"authority": result["count"]},
        "probe": "ok",
    }


def authority_inbox_notice(recipient: str | None, *, file: TextIO | None = None) -> None:
    """Print one line pointing a task-record inbox reader at its Fleet Comms deliveries.

    Body-free: only the queued count is read. An unknown recipient or store
    reports ``unknown``, never zero.
    """
    from lu_machinery.fleet_comms.authority import pending_delivery_snapshot

    slot = (recipient or "").strip().lower()
    result = pending_delivery_snapshot(slot) if slot else {"probe": "unknown"}
    count = result["count"] if result["probe"] == "ok" else "unknown"
    target = slot or "<slot>"
    print(
        f"Fleet Comms deliveries for {target}: {count} queued; read them with "
        f"`{shlex.quote(sys.executable)} -I -m {_MACHINERY}.fleet_comms deliveries receive "
        f"--recipient {target} --worker-id {target}`.",
        file=file or sys.stdout,
    )


def _reject_constant(_value: str) -> None:
    raise ValueError("invalid_json")


def hook(stdin_text: str, recipient: str) -> dict:
    """Render one bounded ephemeral announcement, or silence for proven zero."""
    try:
        payload = json_depth.loads(stdin_text, parse_constant=_reject_constant, max_depth=512)
        if not isinstance(payload, dict):
            raise ValueError
        result = probe(recipient)
        if result["probe"] != "ok":
            message = UNKNOWN_LINE
        elif not result["count"]:
            return {}
        else:
            suffix = ""
            if result["stores"]["authority"]:
                suffix += f"; authority: {_AUTHORITY_HELP}"
            prefix = f"PENDING ORDERS: {result['count']} (ids "
            shown = []
            for identifier in result["ids"]:
                candidate = [*shown, str(identifier)]
                ending = "; truncated)" if len(candidate) < result["count"] else ")"
                if len(prefix + ",".join(candidate) + ending + suffix) > 200:
                    break
                shown = candidate
            truncated = len(shown) < result["count"]
            message = prefix + ",".join(shown) + ("; truncated" if truncated else "") + ")" + suffix
            if not shown:
                message = UNKNOWN_LINE
    except Exception:
        message = UNKNOWN_LINE
    return {"injectSteps": [{"ephemeralMessage": message}]}


class _SilentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ValueError("invalid_arguments")


def main(argv: list[str] | None = None) -> int:
    # Named as it was started: by its import name under ``-m``, else by its file.
    entry = f"-m {__spec__.name}" if __spec__ is not None else os.fspath(__file__)
    parser = _SilentParser(
        description="Report pending orders without bodies or writes.\nUse for per-call announcements, never to consume or acknowledge orders.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f'Examples:\n  .venv/bin/python {entry} --recipient "$SESSION_HANDOFF_AGENT"\n'
            f"  .venv/bin/python {entry} --agy-hook < hook-input.json\n"
            "Outputs: JSON on stdout; no files or database writes.\n"
            "Exit codes: 0 including unknown probes; argparse help exits 0.\n"
            "Related: #10558, docs/agent-runtime-guide.md."
        ),
    )
    parser.add_argument(
        "--recipient", default=None, help="Registered provider-lane handoff slot (default: SESSION_HANDOFF_AGENT)"
    )
    parser.add_argument(
        "--agy-hook",
        action="store_true",
        help="Read AGY JSON stdin and emit injectSteps (default: emit probe metadata)",
    )
    try:
        args = parser.parse_args(argv)
        recipient = args.recipient if args.recipient is not None else os.environ.get("SESSION_HANDOFF_AGENT", "")
        if args.agy_hook:
            stdin_text = sys.stdin.read()
            # The direct hook boundary: missing roots come from the working directory by the
            # shared shell rule. A context that cannot resolve is a bootstrap refusal, not an
            # "unknown" store read.
            try:
                configure_native_hook_roots(Path.cwd())
            except (OSError, ValueError):
                sys.stderr.write(HOOK_BOOTSTRAP_DIAGNOSTIC + "\n")
                result = {"decision": "deny", "reason": HOOK_BOOTSTRAP_REASON}
            else:
                result = hook(stdin_text, recipient)
        else:
            result = probe(recipient)
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        result = hook("", "") if "--agy-hook" in (argv if argv is not None else sys.argv[1:]) else probe("")
    except Exception:
        result = hook("", "") if "--agy-hook" in (argv if argv is not None else sys.argv[1:]) else probe("")
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
