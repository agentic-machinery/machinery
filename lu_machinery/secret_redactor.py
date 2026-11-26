"""Best-effort secret redaction for agent egress paths.

Values assigned to recognized secret names are always redacted, including YAML
pairs and spaced code expressions. Punctuation cannot establish that a value
is safe to publish.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from itertools import pairwise
from typing import Any

REDACTION = "[REDACTED_SECRET]"

_KEY_NAME = r"[A-Za-z_][A-Za-z0-9_]*"

_ASSIGNMENT_PREFIX = re.compile(
    rf"(?<!\w)(?P<key_quote>['\"]?)(?P<key>{_KEY_NAME})(?P=key_quote)\s*(?P<separator>[:=])\s*",
    re.IGNORECASE,
)
_QUOTED_VALUES = {
    '"': re.compile(r'"(?:\\[\s\S]|[^"\\])*"'),
    "'": re.compile(r"'(?:''|[^'])*'"),
}
_SINGLE_QUOTED_EQUALS_VALUE = re.compile(r"'(?:\\[\s\S]|''|[^'\\])*'")
_LINE_END = re.compile(r"[\r\n]")

# A private-key block runs from a BEGIN header to the first END footer after
# it. Header and footer are searched separately: one lazy regex rescans to the
# end of the text from every unterminated header, which is quadratic.
_BLOCK_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_BLOCK_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")

_TOKEN_PATTERNS = (
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:key_[0-9A-Fa-f]{64}|crsr_[A-Za-z0-9]{40,})\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
)
# A JWT's first segment ends at the first character outside its class, so every
# "eyJ" start inside one run shares that run's outcome. One regex retries each
# start and rescans the run, which is quadratic; _redact_jwts skips the run.
_JWT_START = re.compile(r"\beyJ")
_JWT_REST = re.compile(r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
_JWT_SEGMENT_END = re.compile(r"[^A-Za-z0-9_-]")

# A non-vendor secret can appear inside an OAuth/provider error message without
# a secret-named key. Restrict the generic detector to long ASCII token runs so
# natural-language prose is not treated as a credential. Punctuated candidates
# need substantially more entropy because prose commonly has dates, versions,
# and hyphenated title case; unpunctuated alphanumeric/letter-only candidates
# have separate thresholds for their narrower alphabets.
_OPAQUE_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_+./~=-])(?P<value>[A-Za-z0-9_+./~-]{32,})(?:={1,2})?"
    r"(?![A-Za-z0-9_+./~=-])"
)
_OPAQUE_TOKEN_MIN_ENTROPY = 3.5
_OPAQUE_ALPHA_TOKEN_MIN_ENTROPY = 4.0
_OPAQUE_PUNCTUATED_TOKEN_MIN_ENTROPY = 4.5
_OPAQUE_TOKEN_PUNCTUATION = frozenset("_+./~-")

_URL_SCHEME = r"\b[A-Za-z][A-Za-z0-9+.-]*://"
# First bound the whole token; URL boundaries within it depend on userinfo.
# Only anchored matches use it: a search retries every word boundary in a long
# scheme-character run and is quadratic, so _find_scheme locates schemes.
URL_PATTERN = re.compile(rf"{_URL_SCHEME}[^\s<>\"`]*")
_URL_TOKEN_END = re.compile(r"[\s<>\"`]")
_SCHEME_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.-"
_SCHEME_START = re.compile(r"\b[A-Za-z]")
_AUTHORITY_END = re.compile(r"[/?#]")
# RFC 3986 unreserved, sub-delims and pct-encoded, excluding the password colon.
_URL_USERNAME_PATTERN = re.compile(r"(?:[A-Za-z0-9._~!$&'()*+,;=-]|%[0-9A-Fa-f]{2})*")


def _find_scheme(text: str, pos: int, endpos: int) -> tuple[int, int] | None:
    """Return ``(start, end)`` of ``re.search(_URL_SCHEME, text[:endpos], pos)``.

    The scan is linear in the searched span. A scheme run cannot contain ":",
    so the run ending at each "://" is the only place a scheme for it starts,
    and runs before successive separators are disjoint.
    """
    floor = pos
    while (separator := text.find("://", floor, endpos)) >= 0:
        run_start = floor + len(text[floor:separator].rstrip(_SCHEME_CHARS))
        start = _SCHEME_START.search(text, run_start, separator)
        if start is not None:
            return start.start(), separator + 3
        floor = separator + 3
    return None


def _first_index(pattern: re.Pattern[str], text: str, start: int, stop: int) -> int:
    found = pattern.search(text, start, stop)
    return found.start() if found else stop


def _authority_span(text: str, cursor: int, stop: int, at_stop: int) -> tuple[int, int]:
    """Absolute authority bounds of the URL ``text[cursor:stop]``.

    ``at_stop`` bounds the fallback "@" search; any value past the last "@"
    before ``stop`` gives the result for ``stop``.
    """
    start = text.index("://", cursor, stop) + 3
    end = _first_index(_AUTHORITY_END, text, start, stop)
    if text.find("@", start, end) < 0:
        username, colon, password_prefix = text[start:end].partition(":")
        if (
            colon
            and _URL_USERNAME_PATTERN.fullmatch(username)
            and password_prefix
            and not password_prefix.isdigit()
        ):
            userinfo_end = text.find("@", end, at_stop)
            if userinfo_end >= 0:
                end = _first_index(_AUTHORITY_END, text, userinfo_end + 1, stop)
    return start, end


def _url_authority_bounds(raw: str) -> tuple[int, int]:
    """Resolve authority, including the guarded malformed-userinfo fallback."""
    return _authority_span(raw, 0, len(raw), len(raw))


def iter_url_matches(text: str) -> Iterator[re.Match[str]]:
    """Split token URLs only after resolving the current URL's userinfo.

    A scheme inside a password cannot become a new URL. Comma/semicolon
    joiners after resolved userinfo remain outside matches, as in round b.
    Each step scans forward from the previous one, so the pass is linear.
    """
    position = 0
    while (scheme := _find_scheme(text, position, len(text))) is not None:
        cursor = scheme[0]
        token_end = _first_index(_URL_TOKEN_END, text, scheme[1], len(text))
        # One reverse scan per token keeps every fallback "@" search bounded.
        at_stop = text.rfind("@", cursor, token_end) + 1
        while True:
            start, end = _authority_span(text, cursor, token_end, at_stop)
            boundary_floor = max(start, text.rfind("@", start, end) + 1)
            scheme = _find_scheme(text, boundary_floor, token_end)
            limit = token_end
            if scheme is not None:
                joined = scheme[0] > boundary_floor and text[scheme[0] - 1] in ",;"
                limit = scheme[0] - 1 if joined else scheme[0]
            yield URL_PATTERN.match(text, cursor, limit)
            if scheme is None:
                break
            cursor = scheme[0]
        position = token_end


def redact_url_authority(raw: str) -> tuple[str, str, str]:
    """Split a scheme:// URL token into prefix, redacted authority and suffix.

    RFC 3986 section 3.2 ends authority at the first /, ? or #. Only an @
    within that authority normally identifies userinfo; replace everything
    after its first colon up to @, preserving username, host and suffix bytes.

    Diagnostics also contain malformed passwords with raw /, ? or #. If the
    initial authority has no @, accept the first later @ in this same token
    only when the initial authority starts with an RFC username and colon,
    followed by a nonempty, nonnumeric password prefix. Extend authority to
    the first /, ? or # after that @. Numeric and empty prefixes stay ports:
    ordinary host:port URLs cannot trigger this fallback, even with @ in a
    path, query or fragment. Bracketed IP hosts cannot match the username.

    Deliberately leave ambiguous numeric/empty prefixes before a delimiter,
    username-only userinfo, schemeless credentials and tokens without @ alone.
    The shared iterator resolves userinfo before bounding at a later scheme.
    No decoding or normalization occurs.
    """
    start, end = _url_authority_bounds(raw)
    authority = raw[start:end]
    userinfo, separator, host = authority.rpartition("@")
    if separator:
        username, colon, _password = userinfo.partition(":")
        if colon:
            authority = username + ":" + REDACTION + "@" + host
    return raw[:start], authority, raw[end:]


def redact_text(value: str | None) -> str | None:
    """Redact token-looking substrings from text before external egress."""
    if value is None:
        return None

    text = str(value)
    parts = []
    cursor = 0
    for match in iter_url_matches(text):
        parts.append(text[cursor:match.start()])
        parts.extend(redact_url_authority(match.group()))
        cursor = match.end()
    parts.append(text[cursor:])
    text = _redact_blocks("".join(parts))
    text = _redact_assignments(text)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTION, text)
    text = _redact_jwts(text)
    text = _OPAQUE_TOKEN_PATTERN.sub(_redact_opaque_token_match, text)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside JSON-like values."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            key: REDACTION if _is_secret_key(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


def _is_secret_key(key: str) -> bool:
    parts = [part for part in re.split(r"[^A-Z0-9]+", key.upper()) if part]
    if not parts:
        return False

    if any(part in {"TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "CREDENTIALS"} for part in parts):
        return True
    if "PASS" in parts:
        return True

    pairs = set(pairwise(parts))
    if ("PRIVATE", "KEY") in pairs or ("ACCESS", "KEY") in pairs:
        return True
    if len(parts) >= 2 and parts[-2:] == ["API", "KEY"]:
        return True
    return bool(parts and parts[-1] == "APIKEY")


def _shannon_entropy(value: str) -> float:
    """Return the per-character Shannon entropy for an ASCII token candidate."""
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length)
        for count in Counter(value).values()
    )


def _looks_like_opaque_secret(value: str) -> bool:
    """True for a high-entropy opaque token rather than ordinary prose."""
    if not any(char.isalpha() for char in value):
        return False

    entropy = _shannon_entropy(value)
    if _OPAQUE_TOKEN_PUNCTUATION.intersection(value):
        return entropy >= _OPAQUE_PUNCTUATED_TOKEN_MIN_ENTROPY
    if not any(char.isdigit() for char in value):
        return entropy >= _OPAQUE_ALPHA_TOKEN_MIN_ENTROPY
    return entropy >= _OPAQUE_TOKEN_MIN_ENTROPY


def _redact_opaque_token_match(match: re.Match[str]) -> str:
    """Redact only generic candidates that satisfy the entropy detector."""
    return REDACTION if _looks_like_opaque_secret(match.group("value")) else match.group(0)


def _redact_blocks(text: str) -> str:
    """Replace each BEGIN...END private-key block, leftmost first.

    Once a header has no footer after it, no later header can have one.
    """
    parts = []
    cursor = 0
    while (begin := _BLOCK_BEGIN.search(text, cursor)) and (end := _BLOCK_END.search(text, begin.end())):
        parts.append(text[cursor:begin.start()])
        parts.append(REDACTION)
        cursor = end.end()
    parts.append(text[cursor:])
    return "".join(parts)


def _redact_jwts(text: str) -> str:
    """Replace each JWT-shaped token, leftmost first."""
    parts = []
    cursor = position = 0
    while start := _JWT_START.search(text, position):
        rest = _JWT_REST.match(text, start.end())
        if rest is None:
            position = _first_index(_JWT_SEGMENT_END, text, start.end(), len(text))
            continue
        parts.append(text[cursor:start.start()])
        parts.append(REDACTION)
        cursor = position = rest.end()
    parts.append(text[cursor:])
    return "".join(parts)


def _redact_assignments(text: str) -> str:
    parts = []
    cursor = 0
    authorities = iter(
        (match.start() + start, match.start() + end)
        for match in iter_url_matches(text)
        for start, end in (_url_authority_bounds(match.group()),)
    )
    authority = next(authorities, (len(text), len(text)))
    for match in _ASSIGNMENT_PREFIX.finditer(text):
        if match.start() < cursor or not _is_secret_key(match.group("key")):
            continue
        while match.start() >= authority[1]:
            authority = next(authorities, (len(text), len(text)))
        if (match.group("separator") == ":"
                and authority[0] <= match.start() < match.end() < authority[1]):
            tail = text[match.end():authority[1]]
            if (tail.isascii() and tail.isdecimal()) or tail.startswith(REDACTION + "@"):
                continue
        start = match.end()
        if start == len(text):
            continue
        quote = text[start] if text[start] in _QUOTED_VALUES else ""
        if quote:
            pattern = _QUOTED_VALUES[quote]
            if quote == "'" and match.group("separator") == "=":
                pattern = _SINGLE_QUOTED_EQUALS_VALUE
            value = pattern.match(text, start)
            end = value.end() if value else len(text)
            closing = quote if value else ""
        else:
            newline = _LINE_END.search(text, start)
            end = newline.start() if newline else len(text)
            closing = ""
        parts.append(text[cursor:match.start()])
        parts.append(match.group() + quote + REDACTION + closing)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)
