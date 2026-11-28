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
    spec = raw["formatters"]["default"]
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
