"""sd-daemon priority prefixes for lines journald reads from stdout.

journald's default syslog-level prefix treats a leading ``<N>`` as the priority
and strips it. It also starts a new entry at every NUL and at the default
LineMax (48 KiB), and that entry has no prefix of ours. Logging turns one
record into many stream lines when the message or the traceback contains
newlines, so only the first line keeps the record's priority. Later lines,
and every chunk of a line that would cross LineMax, are ``<7>`` (debug).
Embedded NULs are escaped so they cannot split an entry.
"""

from __future__ import annotations

import logging

_ERROR = 3
_WARNING = 4
_INFO = 6
_DEBUG = 7
# journald.conf LineMax default. The ``<N>`` prefix counts toward it.
_LINE_MAX = 48 * 1024
_PREFIX_BYTES = 3


def priority_for_level(level: int) -> int:
    """Map a logging level to an sd-daemon priority: error 3, warning 4, info 6, debug 7."""
    if level >= logging.ERROR:
        return _ERROR
    if level >= logging.WARNING:
        return _WARNING
    if level >= logging.INFO:
        return _INFO
    return _DEBUG


def _journal_text(text: str) -> str:
    """Text journald can store: no raw NUL, no unpaired surrogate.

    A Linux filename that is not UTF-8 arrives as a Python surrogate escape.
    Strict UTF-8 encoding would raise and drop the record.
    """
    text = text.replace("\x00", "\\x00")
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _chunks(payload: str, priority: int) -> list[str]:
    """Prefix one logical line, splitting so each raw line stays within LineMax."""
    limit = _LINE_MAX - _PREFIX_BYTES
    encoded = payload.encode()
    if len(encoded) <= limit:
        return [f"<{priority}>{payload}"]
    chunks: list[str] = []
    first = True
    view = encoded
    while view:
        take = view[:limit]
        while True:
            try:
                text = take.decode()
            except UnicodeDecodeError:
                take = take[:-1]
                if not take:
                    raise ValueError("journal line contains an undecodable byte") from None
            else:
                break
        chunks.append(f"<{priority if first else _DEBUG}>{text}")
        view = view[len(take) :]
        first = False
    return chunks


def prefix_lines(text: str, priority: int) -> str:
    """Prefix the first line with ``<priority>`` and each later line with ``<7>``.

    ``priority`` must be an integer 0 through 7. A bool is refused: it is an int
    subclass, and ``True`` would otherwise become priority 1. NUL bytes and
    unpaired surrogates are escaped, and a line longer than journald's default
    LineMax is split, so every entry journald creates still starts with a
    prefix we chose.
    """
    if type(priority) is not int or not 0 <= priority <= 7:
        raise ValueError(f"journal priority must be an integer 0..7, got {priority!r}")
    keep_newline = text.endswith("\n")
    body = text[:-1] if keep_newline else text
    body = _journal_text(body)
    lines = body.split("\n")
    prefixed_lines: list[str] = []
    for index, line in enumerate(lines):
        prefixed_lines.extend(_chunks(line, priority if index == 0 else _DEBUG))
    prefixed = "\n".join(prefixed_lines)
    if keep_newline:
        prefixed += "\n"
    return prefixed


class JournalSeverityFormatter(logging.Formatter):
    """logging formatter that emits the sd-daemon priority of each record."""

    def format(self, record: logging.LogRecord) -> str:
        return prefix_lines(super().format(record), priority_for_level(record.levelno))
