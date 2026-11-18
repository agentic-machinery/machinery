"""CTO seat safe mode: refuse new write dispatches and landing from the CTO seat.

The CTO supervisor writes the safe-mode file (``config.CTO_SAFE_MODE_FILE``) after two consecutive fast
deaths of the CTO seat and clears it only with its own clear command. While the file exists:

* a new write dispatch whose caller is the CTO seat is refused (read-only dispatches still run);
* the publisher's ``pr-merge`` (enqueue) from the CTO seat is refused.

Dispatched workers are never the CTO seat, so their own chains (dispatch and landing) keep running.
A file that exists but cannot be read or validated refuses with its own reason (fail closed); only a
missing file means "not in safe mode". The file is read fresh on every call, so a subprocess of the
CTO session sees the same state as the session itself.

The CTO seat is identified by the launcher's environment: ``start-claude-cto.sh`` exports
``SESSION_SEAT=cto`` and every epics-lane driver exports ``SESSION_EPIC=epics``. A dispatched worker's
environment is an allowlist (``dispatcher.environment.child_env``) that carries neither, and it always
carries ``LEARN_UKRAINIAN_DISPATCH_TASK_ID``; a process inside a write boundary carries the broker
variable. Either marker makes the caller a worker, never the CTO seat. A dispatch may also name the
CTO seat explicitly with ``--initiator claude-cto`` (or a session id ``claude-cto-<id>``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from lu_machinery import config
from lu_machinery.common import json_depth
from lu_machinery.common.safe_open import safe_open_below

CTO_SEAT = "claude-cto"
_WORKER_MARKERS = ("LEARN_UKRAINIAN_DISPATCH_TASK_ID", "LU_WRITE_BOUNDARY_BROKER")
_FIELDS = {"set_at": str, "reason": str, "generation": int, "outage_id": str}
_MAX_BYTES = 64 * 1024
_UNPRINTABLE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class SafeModeError(ValueError):
    """The safe-mode file exists but cannot be read or validated."""


def safe_mode_file() -> Path:
    """The one configured location, expanded against the caller's home directory."""
    return Path(config.CTO_SAFE_MODE_FILE).expanduser()


def is_cto_caller(environment: Mapping[str, str], initiator: str | None = None) -> bool:
    """True when the caller is the CTO seat, by explicit initiator or by its launcher environment."""
    name = " ".join(str(initiator or "").split())
    if name == CTO_SEAT or name.startswith(f"{CTO_SEAT}-"):
        return True
    if any(environment.get(marker) for marker in _WORKER_MARKERS):
        return False
    return environment.get("SESSION_SEAT") == "cto" or environment.get("SESSION_EPIC") == "epics"


def _read_bounded(path: Path) -> bytes:
    """At most ``_MAX_BYTES + 1`` bytes of a regular file we own, opened without following or blocking.

    Only a missing parent directory or a missing entry raises ``FileNotFoundError``.
    """
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fd = safe_open_below(directory, path.name, os.O_RDONLY)
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as handle:
        return handle.read(_MAX_BYTES + 1)


def read_state(path: Path | None = None) -> dict | None:
    """``None`` when the file is absent; the validated record when present; ``SafeModeError`` otherwise."""
    path = path or safe_mode_file()
    try:
        raw = _read_bounded(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        # A symlink (dangling or not), FIFO, directory or foreign-owned entry is present but no state record.
        raise SafeModeError(f"cannot be read ({type(exc).__name__})") from None
    if len(raw) > _MAX_BYTES:
        raise SafeModeError(f"is larger than {_MAX_BYTES} bytes")
    try:
        data = json_depth.loads(raw.decode("utf-8"), max_depth=8)
    except (UnicodeError, ValueError, RecursionError):
        raise SafeModeError("is not a JSON object") from None
    if not isinstance(data, dict):
        raise SafeModeError("is not a JSON object")
    for key, kind in _FIELDS.items():
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, kind) or (isinstance(value, str) and not value.strip()):
            raise SafeModeError(f"has no valid {key}")
    return data


def _clean(value: object) -> str:
    return _UNPRINTABLE.sub(" ", str(value))[:200]


def refusal(action: str, environment: Mapping[str, str], initiator: str | None = None) -> str | None:
    """The typed refusal for ``action`` by the CTO seat while in safe mode, else ``None``."""
    if not is_cto_caller(environment, initiator):
        return None
    try:
        state = read_state()
    except SafeModeError as exc:
        return (f"CTO_SAFE_MODE_UNREADABLE: {action} refused for the CTO seat: the safe-mode file "
                f"{config.CTO_SAFE_MODE_FILE} {exc}; refusing until the supervisor rewrites or clears it")
    if state is None:
        return None
    return (f"CTO_SAFE_MODE: {action} refused for the CTO seat while {config.CTO_SAFE_MODE_FILE} exists "
            f"(reason: {_clean(state['reason'])}; outage {_clean(state['outage_id'])}, generation "
            f"{state['generation']}, set at {_clean(state['set_at'])}); dispatched workers are unaffected; "
            "only the supervisor's clear command ends safe mode")
