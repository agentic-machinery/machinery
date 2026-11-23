"""Generate :mod:`lu_machinery.agent_runtime.unicode_printable` from the supported interpreters' tables.

The no-Sources consumer path must classify text identically on every supported Python, but each
CPython ships the ``unicodedata`` table of its own Unicode version (3.12: 15.0.0, 3.13: 15.1.0,
3.14: 16.0.0). The generated module records, per runtime table, the code-point ranges whose
printability differs from Unicode 15.0.0, so
:func:`~lu_machinery.agent_runtime.sources_read_only.printable_text` gives the Unicode 15.0.0
answer on every supported table and refuses a table it holds no correction for.

A code point is printable when it is the ASCII space or its general category is outside ``C*``
(control, format, surrogate, private use, unassigned) and ``Z*`` (separator). A table reduces to a
bitmap of ``0x110000`` bits (code point ``cp`` at byte ``cp >> 3``, bit ``cp & 7``); its SHA-256
identifies the table's printability.

Inputs are the interpreters named with ``--python``. Each runs with ``-I -S`` and imports only
``json``, ``sys`` and ``unicodedata``. Exactly one Unicode 15.0.0 table is required, and several
interpreters may share a Unicode version only if their bitmaps are identical. The output records
each input's Python minor version, ``unidata_version`` and bitmap digest, never its path or patch
release, so it is a pure function of the tables: any patch release of the same minor versions
regenerates it byte for byte (``--check``), and a table that differs changes its digest.

Usage, from the checkout root (``--write`` replaces the sibling module of the imported generator)::

    python -m lu_machinery.agent_runtime.unicode_printable_gen \\
        --python <python3.12> --python <python3.13> --python <python3.14> --write

``--verify-runtime`` instead checks the running interpreter: ``printable_text`` over every code
point must reproduce the recorded Unicode 15.0.0 bitmap digest.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from lu_machinery.common import json_depth

CODE_POINTS = 0x110000
BASELINE_UNIDATA_VERSION = "15.0.0"
OUTPUT = Path(__file__).with_name("unicode_printable.py")
# The dump is an object holding a list of [lo, hi] lists.
_DUMP_DEPTH = 3

# Runs in each input interpreter with -I -S: the printable ranges of its own unicodedata table.
_DUMP = """\
import json, sys, unicodedata
ranges = []
for cp in range(0x110000):
    ch = chr(cp)
    if ch == " " or unicodedata.category(ch)[0] not in "CZ":
        if ranges and ranges[-1][1] == cp - 1:
            ranges[-1][1] = cp
        else:
            ranges.append([cp, cp])
json.dump(
    {"python": "%d.%d" % sys.version_info[:2], "unidata_version": unicodedata.unidata_version, "printable": ranges},
    sys.stdout,
)
"""


def ranges_bitmap(ranges: Iterable[Iterable[int]]) -> bytes:
    """The printability bitmap of inclusive ``(lo, hi)`` code-point ranges."""
    bits = bytearray(CODE_POINTS // 8)
    for lo, hi in ranges:
        if not 0 <= lo <= hi < CODE_POINTS:
            raise ValueError(f"code-point range out of bounds: {lo}..{hi}")
        for cp in range(lo, hi + 1):
            bits[cp >> 3] |= 1 << (cp & 7)
    return bytes(bits)


def predicate_bitmap(printable: Callable[[str], bool]) -> bytes:
    """The printability bitmap of *printable* called on every code point."""
    bits = bytearray(CODE_POINTS // 8)
    for cp in range(CODE_POINTS):
        if printable(chr(cp)):
            bits[cp >> 3] |= 1 << (cp & 7)
    return bytes(bits)


def _ranges(code_points: Iterable[int]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for cp in code_points:
        if out and out[-1][1] == cp - 1:
            out[-1][1] = cp
        else:
            out.append([cp, cp])
    return [(lo, hi) for lo, hi in out]


def _quote(text: str) -> str:
    """*text* (dotted digits or hex only) as a double-quoted literal, as Ruff formats it."""
    return f'"{text}"'


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _dump(interpreter: str) -> tuple[str, str, bytes]:
    """``(python_version, unidata_version, bitmap)`` of *interpreter*'s table."""
    proc = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        [interpreter, "-I", "-S", "-c", _DUMP], capture_output=True, check=True, timeout=600
    )
    data = json_depth.loads(proc.stdout, max_depth=_DUMP_DEPTH)
    if not isinstance(data, dict) or set(data) != {"python", "unidata_version", "printable"}:
        raise SystemExit(f"{interpreter}: unexpected table dump")
    python, version, printable = data["python"], data["unidata_version"], data["printable"]
    if not (
        isinstance(python, str)
        and isinstance(version, str)
        and all(part.isdigit() and part.isascii() for text in (python, version) for part in text.split("."))
        and isinstance(printable, list)
        and all(isinstance(pair, list) and len(pair) == 2 and all(type(cp) is int for cp in pair) for pair in printable)
    ):
        raise SystemExit(f"{interpreter}: unexpected table dump")
    return python, version, ranges_bitmap(printable)


def render(interpreters: Iterable[str]) -> str:
    """The generated module's text for the tables of *interpreters*."""
    tables: dict[str, bytes] = {}
    inputs: list[tuple[str, str, str]] = []
    for interpreter in interpreters:
        python, version, bits = _dump(interpreter)
        if tables.setdefault(version, bits) != bits:
            raise SystemExit(f"Python {python}: Unicode {version} table differs from another {version} input")
        inputs.append((python, version, hashlib.sha256(bits).hexdigest()))
    if BASELINE_UNIDATA_VERSION not in tables:
        raise SystemExit(f"no Unicode {BASELINE_UNIDATA_VERSION} input interpreter (CPython 3.12)")
    base = tables[BASELINE_UNIDATA_VERSION]
    lines = [
        '"""Unicode 15.0.0 printability corrections for the supported runtime ``unicodedata`` tables.',
        "",
        "GENERATED by :mod:`lu_machinery.agent_runtime.unicode_printable_gen`; do not edit. ``CORRECTIONS``",
        "maps a runtime ``unicodedata.unidata_version`` to the inclusive code-point ranges whose",
        "printability (the ASCII space, or a general category outside ``C*`` and ``Z*``) differs from",
        "Unicode 15.0.0; a version without an entry has no verified correction.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f"BASELINE_UNIDATA_VERSION = {_quote(BASELINE_UNIDATA_VERSION)}",
        "# SHA-256 of the Unicode 15.0.0 printability bitmap (0x110000 bits, code point cp at byte cp >> 3, bit cp & 7).",
        f"BASELINE_SHA256 = {_quote(hashlib.sha256(base).hexdigest())}",
        "# Generator inputs: (Python minor version, unidata_version, SHA-256 of that table's printability bitmap).",
        "GENERATOR_INPUTS: tuple[tuple[str, str, str], ...] = (",
        *(
            f"    ({', '.join(map(_quote, entry))}),"
            for entry in sorted(inputs, key=lambda e: (_version_key(e[1]), _version_key(e[0])))
        ),
        ")",
        "CORRECTIONS: dict[str, tuple[tuple[int, int], ...]] = {",
    ]
    for version in sorted(tables, key=_version_key):
        bits = tables[version]
        ranges = _ranges(cp for cp in range(CODE_POINTS) if (bits[cp >> 3] ^ base[cp >> 3]) >> (cp & 7) & 1)
        if not ranges:
            lines.append(f"    {_quote(version)}: (),")
            continue
        lines.append(f"    {_quote(version)}: (")
        lines.extend(f"        (0x{lo:04X}, 0x{hi:04X})," for lo, hi in ranges)
        lines.append("    ),")
    lines.append("}")
    return "\n".join(lines) + "\n"


def verify_runtime() -> str | None:
    """``None`` when ``printable_text`` reproduces Unicode 15.0.0 on this interpreter, else the reason."""
    import unicodedata

    from lu_machinery.agent_runtime.sources_read_only import printable_text
    from lu_machinery.agent_runtime.unicode_printable import BASELINE_SHA256

    digest = hashlib.sha256(predicate_bitmap(printable_text)).hexdigest()
    if digest != BASELINE_SHA256:
        return f"unicodedata {unicodedata.unidata_version}: corrected bitmap {digest} != baseline {BASELINE_SHA256}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lu_machinery.agent_runtime.unicode_printable_gen",
        description="Generate, check or verify the Unicode 15.0.0 printability corrections.",
    )
    parser.add_argument("--python", action="append", default=[], help="input interpreter, once per Unicode version")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help=f"write {OUTPUT.name}")
    mode.add_argument("--check", action="store_true", help=f"exit 1 unless {OUTPUT.name} regenerates byte for byte")
    mode.add_argument("--verify-runtime", action="store_true", help="check printable_text on this interpreter")
    args = parser.parse_args(argv)
    if args.verify_runtime:
        if args.python:
            parser.error("--verify-runtime takes no --python")
        reason = verify_runtime()
        print(reason or "ok")
        return 1 if reason else 0
    if not args.python:
        parser.error("--write and --check need --python")
    text = render(args.python)
    if args.write:
        OUTPUT.write_text(text, encoding="utf-8")
        return 0
    if OUTPUT.read_text(encoding="utf-8") != text:
        print(f"{OUTPUT.name} differs from its regeneration: run --write", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
