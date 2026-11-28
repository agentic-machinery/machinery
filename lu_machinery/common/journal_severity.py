"""sd-daemon priority prefixes for lines journald reads from stdout.

journald's default syslog-level prefix treats a leading ``<N>`` as the priority
and strips it. Logging turns one record into many stream lines when the message
or the traceback contains newlines, so only the first line keeps the record's
priority. Later lines are ``<7>`` (debug) and do not count as further errors
or warnings.
"""

from __future__ import annotations

import logging

_ERROR = 3
_WARNING = 4
_INFO = 6
_DEBUG = 7


def priority_for_level(level: int) -> int:
    """Map a logging level to an sd-daemon priority: error 3, warning 4, info 6, debug 7."""
    if level >= logging.ERROR:
        return _ERROR
    if level >= logging.WARNING:
        return _WARNING
    if level >= logging.INFO:
        return _INFO
    return _DEBUG


def prefix_lines(text: str, priority: int) -> str:
    """Prefix the first line with ``<priority>`` and each later line with ``<7>``.

    ``priority`` must be an integer 0 through 7. A bool is refused: it is an int
    subclass, and ``True`` would otherwise become priority 1.
    """
    if type(priority) is not int or not 0 <= priority <= 7:
        raise ValueError(f"journal priority must be an integer 0..7, got {priority!r}")
    keep_newline = text.endswith("\n")
    body = text[:-1] if keep_newline else text
    lines = body.split("\n")
    prefixed = "\n".join(f"<{priority if index == 0 else _DEBUG}>{line}" for index, line in enumerate(lines))
    if keep_newline:
        prefixed += "\n"
    return prefixed


class JournalSeverityFormatter(logging.Formatter):
    """logging formatter that emits the sd-daemon priority of each record."""

    def format(self, record: logging.LogRecord) -> str:
        return prefix_lines(super().format(record), priority_for_level(record.levelno))
