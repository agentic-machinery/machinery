"""JSON parsing with an explicit nesting-depth limit.

CPython 3.12 refuses deeply nested JSON only by accident: its C decoder and encoder
count recursion and raise ``RecursionError`` at about 10,000 levels. CPython 3.14 replaced
that count with C-stack protection, so the same input now parses (a 50,000-level array
does). A reader that treated ``RecursionError`` as its refusal accepts that input on 3.14.

These helpers make the limit explicit and identical on every interpreter. Each caller
passes the ``max_depth`` it documents; :data:`MAX_DEPTH_CEILING` keeps every limit below
the depth at which 3.12's decoder would raise ``RecursionError`` itself.

Depth counts containers: ``[]`` and ``{}`` are depth 1, ``[[1]]`` is depth 2, a scalar is 0.

An over-deep document raises :class:`RecursionError`, the exception 3.12's decoder raised for
the same input, now from this explicit check rather than from the interpreter. Every existing
handler therefore keeps its 3.12 control flow and its published code: ``except (ValueError,
RecursionError)`` still refuses, ``except ValueError`` still lets the depth refusal reach the
caller that names it, and a code built from ``type(exc).__name__`` still reads
``RecursionError``. When the document also has a syntax error, the error the decoder meets
first wins, as on 3.12: a syntax error before the depth is exceeded raises that
:class:`json.JSONDecodeError`, and a document that exceeds the depth first raises
:class:`RecursionError`.
"""

from __future__ import annotations

import json
import re
from itertools import accumulate
from typing import Any

__all__ = ["MAX_DEPTH_CEILING", "check_depth", "loads", "raw_decode"]

# Below 3.12's decoder limit (about 9,998 levels at top level) with headroom for the
# caller's own stack, and below the default Python recursion limit (1,000), so code that
# walks an accepted value recursively cannot hit RecursionError either.
MAX_DEPTH_CEILING = 512

# A JSON string token, or one bracket. An unterminated string, including one that ends in a
# lone backslash, runs to the end of the text, so a string token never fails to match and
# never rescans the rest (no quadratic backtracking).
_TOKEN = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*(?:"|\\?\Z)|[\[\]{}]', re.DOTALL)
_OPENERS = "[{"
# Bytes for the C-speed view of the same scan: brackets folded to one pair, quotes, and for
# text with escaped quotes, backslashes plus the marker that replaces each escape pair.
_FOLD = bytes.maketrans(b"{}", b"[]")
_NOT_STRUCTURE = bytes(sorted(set(range(256)) - set(b'[]{}"')))
_NOT_STRUCTURE_OR_ESCAPE = bytes(sorted(set(range(256)) - set(b'[]{}"\\\x00')))
_STEP = {ord("["): 1, ord("]"): -1}.__getitem__
# JSON whitespace, as json.loads skips it around a document (json.decoder.WHITESPACE).
_WHITESPACE = re.compile(r"[ \t\n\r]*")


def _too_deep(max_depth: int, pos: int | None = None) -> RecursionError:
    where = "" if pos is None else f" at char {pos}"
    return RecursionError(f"JSON nesting exceeds {max_depth} levels{where}")


def _skip_whitespace(text: str, pos: int) -> int:
    match = _WHITESPACE.match(text, pos)
    return match.end() if match else pos


def _limit(max_depth: int) -> int:
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 1 <= max_depth <= MAX_DEPTH_CEILING:
        raise ValueError(f"max_depth must be an int from 1 to {MAX_DEPTH_CEILING}, got {max_depth!r}")
    return max_depth


def _first_too_deep(text: str, start: int, max_depth: int) -> int | None:
    """Index of the first opening bracket at depth ``max_depth + 1`` from ``start``, else None.

    Brackets inside string tokens do not count. The scan is exact for any prefix the
    decoder accepts; past the decoder's first error it may differ (an unterminated string
    hides every later bracket), which :func:`_decode` resolves by error position.
    """
    # At most one bracket per level can be open, so a short text cannot be too deep.
    if text.count("[", start) + text.count("{", start) <= max_depth:
        return None
    # A long text is usually shallow: confirm that at C speed before the per-match scan.
    view = _structure_depth(text, start)
    if view is not None and view <= max_depth:
        return None
    depth = 0
    for match in _TOKEN.finditer(text, start):
        token = match.group()
        if token in _OPENERS:
            depth += 1
            if depth > max_depth:
                return match.start()
        elif token in "]}":
            depth -= 1
    return None


def _structure_depth(text: str, start: int) -> int | None:
    """The scan's deepest nesting from ``start``, from C-speed bytes operations; None if unsure.

    UTF-8 keeps every ASCII character as one byte and puts no ASCII byte in a multi-byte
    character. When no quote follows a backslash, every quote opens or closes a string, as
    in the scan, so splitting on quotes leaves the text between strings at even indexes.

    Otherwise each escaped backslash and then each escaped quote becomes a NUL marker, which
    leaves exactly the string delimiters as quotes up to the first backslash outside a
    string. That backslash then stays outside a string as a backslash or a marker, so any
    backslash or NUL found there returns None and the caller runs the scan itself.
    """
    data = text[start:].encode("utf-8", "surrogatepass")
    if b'\\"' not in data:
        outside = b"".join(data.translate(_FOLD, _NOT_STRUCTURE).split(b'"')[::2])
    else:
        data = data.replace(b"\\\\", b"\x00").replace(b'\\"', b"\x00")
        outside = b"".join(data.translate(_FOLD, _NOT_STRUCTURE_OR_ESCAPE).split(b'"')[::2])
        if b"\\" in outside or b"\x00" in outside:
            return None
    return max(accumulate(map(_STEP, outside), initial=0))


def _decode(text: str, idx: int, max_depth: int, decoder: json.JSONDecoder) -> tuple[Any, int]:
    deep = _first_too_deep(text, idx, max_depth)
    if deep is None:
        # At most max_depth levels: no interpreter's decoder recursion limit is reachable.
        return decoder.raw_decode(text, idx)
    # Decode only through the first too-deep bracket. The decoder then meets, in document
    # order, every syntax error and every hook call for a container closed before that
    # bracket, as 3.12 did before its recursion limit.
    try:
        return decoder.raw_decode(text[: deep + 1], idx)
    except json.JSONDecodeError as exc:
        if exc.pos <= deep:
            raise json.JSONDecodeError(exc.msg, text, exc.pos) from None
    # The decoder accepted the bracket and ran out of text inside it: too deep.
    raise _too_deep(max_depth, deep)


def _document(s: str | bytes | bytearray) -> str:
    if isinstance(s, str):
        return s
    if not isinstance(s, (bytes, bytearray)):
        raise TypeError(f"the JSON object must be str, bytes or bytearray, not {s.__class__.__name__}")
    return s.decode(json.detect_encoding(s), "surrogatepass")


def loads(s: str | bytes | bytearray, *, max_depth: int, **kwargs: Any) -> Any:
    """``json.loads(s, **kwargs)`` that raises :class:`RecursionError` past ``max_depth`` levels."""
    limit = _limit(max_depth)
    text = _document(s)
    if text.startswith("\ufeff"):
        raise json.JSONDecodeError("Unexpected UTF-8 BOM (decode using utf-8-sig)", text, 0)
    decoder = json.JSONDecoder(**kwargs)
    # json.loads skips leading and trailing JSON whitespace around one value.
    start = _skip_whitespace(text, 0)
    value, end = _decode(text, start, limit, decoder)
    end = _skip_whitespace(text, end)
    if end != len(text):
        raise json.JSONDecodeError("Extra data", text, end)
    return value


def raw_decode(
    text: str, idx: int = 0, *, max_depth: int, decoder: json.JSONDecoder | None = None
) -> tuple[Any, int]:
    """``decoder.raw_decode(text, idx)`` that raises :class:`RecursionError` past ``max_depth`` levels."""
    return _decode(text, idx, _limit(max_depth), decoder if decoder is not None else json.JSONDecoder())


def check_depth(value: Any, *, max_depth: int) -> None:
    """Raise :class:`RecursionError` when ``value``'s dicts, lists and tuples nest past ``max_depth``.

    For values about to be serialized. It walks without recursion, so a cyclic value is
    refused at the limit as well.
    """
    limit = _limit(max_depth)
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, dict):
            children: Any = item.values()
        elif isinstance(item, (list, tuple)):
            children = item
        else:
            continue
        depth += 1
        if depth > limit:
            raise _too_deep(limit)
        pending.extend((child, depth) for child in children)
