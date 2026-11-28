"""One journal entry per logging record, at that record's priority."""

from __future__ import annotations

import copy
import json
import logging
import logging.config
import sys

from lu_machinery.common.journal_severity import JournalSeverityFormatter
from lu_machinery.common.repo_root import machinery_path


def test_multiline_record_counts_once():
    """A multi-line record or traceback is one error, and a warning is not info.

    Defect class: a multi-line record or traceback is stored as several entries
    at error level, or a warning is stored at info. Each prefixed line is one
    journal entry under journald's syslog-level prefix.
    """
    raw = json.loads(machinery_path("api/logging.json").read_text(encoding="utf-8"))
    formatters = raw.get("formatters") if type(raw) is dict else None
    spec = formatters.get("default") if type(formatters) is dict else None
    assert type(spec) is dict
    assert spec["()"] == "lu_machinery.common.journal_severity.JournalSeverityFormatter"
    formatter = logging.config.DictConfigurator({"version": 1}).configure_formatter(copy.deepcopy(spec))
    assert isinstance(formatter, JournalSeverityFormatter)

    try:
        raise RuntimeError("disk full")
    except RuntimeError:
        error = formatter.format(
            logging.LogRecord("api.test", logging.ERROR, __file__, 1, "failed\nwhile writing", (), sys.exc_info())
        )
    lines = error.split("\n")
    assert len(lines) >= 3
    assert sum(line.startswith("<3>") for line in lines) == 1
    assert lines[0].startswith("<3>") and "[ERROR]" in lines[0]
    assert all(line.startswith("<7>") for line in lines[1:])
    assert any("Traceback (most recent call last):" in line for line in lines[1:])

    warning = formatter.format(
        logging.LogRecord("api.test", logging.WARNING, __file__, 1, "cache stale\nretrying", (), None)
    ).split("\n")
    assert len(warning) == 2
    assert warning[0].startswith("<4>") and "[WARNING]" in warning[0]
    assert warning[1].startswith("<7>")
    assert not any(line.startswith("<6>") for line in warning)

    # A NUL is a journald entry boundary, and the following text can supply its own priority.
    nul = formatter.format(
        logging.LogRecord("api.test", logging.WARNING, __file__, 1, "hello\x00<3>injected", (), None)
    )
    assert "\x00" not in nul
    nul_lines = nul.split("\n")
    assert len(nul_lines) == 1
    assert nul_lines[0].startswith("<4>")
    assert "\\x00" in nul_lines[0]

    # A line past journald's default 48 KiB LineMax would otherwise continue at info.
    long_lines = formatter.format(
        logging.LogRecord("api.test", logging.ERROR, __file__, 1, "E" * (48 * 1024), (), None)
    ).split("\n")
    assert sum(line.startswith("<3>") for line in long_lines) == 1
    assert long_lines[0].startswith("<3>")
    assert all(line.startswith("<7>") for line in long_lines[1:])
    assert all(len(line.encode()) <= 48 * 1024 for line in long_lines)

    # A non-UTF-8 filename arrives as a surrogate. Strict encoding would drop the record.
    surrogate = formatter.format(
        logging.LogRecord("api.test", logging.WARNING, __file__, 1, "plan \udc80.json unreadable", (), None)
    )
    surrogate.encode("utf-8")
    assert "\udc80" not in surrogate
    assert "\\udc80" in surrogate
    surrogate_lines = surrogate.split("\n")
    assert len(surrogate_lines) == 1
    assert surrogate_lines[0].startswith("<4>")
