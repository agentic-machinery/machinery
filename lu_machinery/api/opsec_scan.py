"""Token-aware OPSEC scanning for Monitor API response values.

The scanner is intentionally a consumer-side utility.  It does not rewrite a
response or make decisions about which response fields are safe; callers pass
the body, header values, and optional ``_telemetry`` value that they want
checked.  Findings retain the operation and leaf field path so a route sweep
can identify the owning emitter without exposing a larger response in its
failure output.
"""

from __future__ import annotations

import bisect
import ipaddress
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote

DEFAULT_OPERATION = "<unknown>"
BOUNDED_FILESYSTEM_ROOTS = (
    "/home",
    "/Users",
    "/Volumes",
    "/private",
    "/opt",
    "/srv",
    "/tmp",
    "/var",
)

_BODY_PATH = "body"
_HEADERS_PATH = "headers"
_TELEMETRY_PATH = "_telemetry"
_MISSING = object()

_SHA_RE = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})(?![0-9A-Fa-f])")
_RFC3339_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
    r"(?![A-Za-z0-9_])"
)
_EPIC_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])epic:[0-9]+(?![A-Za-z0-9_])")
_API_ROUTE_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])/api(?:/[^\s\"'<>]*)?")
# A client-side hash route such as "#/home" never reaches the filesystem. Only a
# bare route name is exempt; "#/home/..." still carries a path tail and is scanned.
_HASH_ROUTE_RE = re.compile(r"(?<![A-Za-z0-9_])#/[a-z][a-z0-9_-]*(?=[\s\"'`<>),;]|$)")
_ISO_DURATION_RE = re.compile(
    r"(?<![A-Za-z0-9_])-?P"
    r"(?=(?:[0-9]+(?:\.[0-9]+)?[YMWD]|T[0-9]+(?:\.[0-9]+)?[HMS]))"
    r"(?:[0-9]+(?:\.[0-9]+)?Y)?"
    r"(?:[0-9]+(?:\.[0-9]+)?M)?"
    r"(?:[0-9]+(?:\.[0-9]+)?W)?"
    r"(?:[0-9]+(?:\.[0-9]+)?D)?"
    r"(?:T(?:[0-9]+(?:\.[0-9]+)?H)?(?:[0-9]+(?:\.[0-9]+)?M)?(?:[0-9]+(?:\.[0-9]+)?S)?)?"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

# This is only a candidate tokenizer.  Whether a candidate is an address is
# decided by ipaddress.ip_address below; the regex must not become a second,
# subtly different IP parser.  Bracketed addresses keep an optional port as a
# single candidate.  Unbracketed IPv4:port is handled by the same parser.
_IP_CANDIDATE_RE = re.compile(r"(?<![A-Za-z0-9])(?:\[[0-9A-Fa-f:.%]+\](?::[0-9]{1,5})?|[0-9A-Fa-f:.]+)(?![A-Za-z0-9])")
# A path finding ends where its whitespace-, quote- or angle-bracket-delimited
# run ends, less trailing prose punctuation and backticks.  Text cannot tell a
# "," ";" "&" or ")" inside a path component from a separator, so the span
# fails closed: it may also cover a glued neighbour, but never leaves path
# bytes behind.
_PATH_RUN_RE = re.compile(r"[^\s\"'<>]+")
_PATH_TRAILING_PUNCTUATION = ".,;:!?)]}`"
# Delimit scan parts independently of the wider redaction span. A comma or
# semicolon can join references in prose as well as form part of a path.
_MARKUP_RE = re.compile(r"`+|(?<!\\)\]\(|[()]")
_PART_END_RE = re.compile(r"[?#&=]|[,;](?=\.{0,2}/)|(?<![A-Za-z0-9])(?=(?i:file):)")
_AUTHORITY_END_RE = re.compile(r"[/#?`]|[,;](?=(?i:file):)")
_SCHEME_RE = re.compile(r"[.!+\[:]*(?P<name>[A-Za-z][A-Za-z0-9+.-]*):")
_AUTHORITY_RE = re.compile(
    r"(?:[A-Za-z0-9._~!$%&'()*+,;=:-]*@)?"
    r"(?:\[(?P<literal>[^\[\]]+)\]|[A-Za-z0-9._~!$%&'()*+,;=-]+)"
    r"(?::[0-9]*)?"
)
_IPVFUTURE_RE = re.compile(
    r"v[0-9A-Fa-f]+\.[A-Za-z0-9._~!$&'()*+,;=:-]+", re.IGNORECASE
)
_URI_PATH_RE = re.compile(r"[A-Za-z0-9._~!$%&'()*+,;=:@/-]*")
_BAD_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_API_SEGMENT_RE = re.compile(r"/api(?=[/?#]|$)", re.IGNORECASE)
_DOT_SEGMENT_RE = re.compile(r"(?<![.\w~%+@$-])\.{1,2}(?=/)")
_ROOT_PREFIX_RE = re.compile(
    "|".join(re.escape(root) for root in BOUNDED_FILESYSTEM_ROOTS)
)
_BARE_NAME_RE = re.compile(r"[A-Za-z0-9_-]")
_RELATIVE_NAME_RE = re.compile(r"[\w~%+@$-]|\.+[\w~%+@$-]")

_USER_AT_HOST_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"(?P<token>[A-Za-z0-9_.-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?)"
    r"(?![A-Za-z0-9_.-])"
)
_SSH_ALIAS_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.@-])ssh[ \t]+"
    r"(?P<alias>[A-Za-z0-9][A-Za-z0-9._-]*)(?![A-Za-z0-9_.:@-])"
)
_HOST_PORT_RE = re.compile(
    r"(?<![A-Za-z0-9_.@-])"
    r"(?P<host>[A-Za-z](?:[A-Za-z0-9.-]*[A-Za-z0-9])?):(?P<port>[0-9]{1,5})"
    r"(?![0-9A-Za-z_.%])"
)
_TRANSCRIPT_FILENAME_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])"
    r"(?P<filename>[A-Za-z0-9][A-Za-z0-9_.-]*\.jsonl(?:\.gz)?)"
    r"(?![A-Za-z0-9_.-])"
)

_SAFE_FIELD_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_SSH_PROSE_WORDS = frozenset(
    {"a", "an", "and", "as", "by", "for", "from", "host", "in", "on", "or", "probes", "the", "to"}
)
_CSS_NUMERIC_PROPERTIES = frozenset(
    {
        "align-items",
        "border",
        "border-bottom",
        "border-bottom-left-radius",
        "border-bottom-right-radius",
        "border-left",
        "box-shadow",
        "border-radius",
        "bottom",
        "column-gap",
        "flex",
        "font-size",
        "font-weight",
        "gap",
        "grid-column",
        "grid-row",
        "height",
        "inset",
        "left",
        "line-height",
        "margin",
        "margin-bottom",
        "margin-left",
        "margin-right",
        "margin-top",
        "max-height",
        "max-width",
        "min-height",
        "min-width",
        "opacity",
        "padding",
        "padding-bottom",
        "padding-left",
        "padding-right",
        "padding-top",
        "right",
        "top",
        "width",
        "z-index",
    }
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One OPSEC match in one string-valued response leaf.

    ``start`` and ``end`` are offsets in the leaf value, not in a serialized
    response.  The operation and field path are deliberately separate: a
    caller can scan the same value for more than one route operation while a
    nested body/header/telemetry path remains unambiguous.
    """

    operation: str
    field_path: str
    kind: str
    token: str
    start: int
    end: int

    @property
    def path(self) -> str:
        """Compatibility alias for callers that call the field path ``path``."""
        return self.field_path

    @property
    def field(self) -> str:
        """Compatibility alias for callers that call the field path ``field``."""
        return self.field_path

    @property
    def value(self) -> str:
        """Compatibility alias for the matched token."""
        return self.token

    @property
    def match(self) -> str:
        """Compatibility alias for the matched token."""
        return self.token

    @property
    def category(self) -> str:
        """Compatibility alias for the finding kind."""
        return self.kind

    @property
    def source(self) -> str:
        """Return the response section containing this finding."""
        return self.field_path.split(".", 1)[0].split("[", 1)[0]

    def as_dict(self) -> dict[str, Any]:
        """Return a small JSON-compatible representation for test reports."""
        return {
            "operation": self.operation,
            "field_path": self.field_path,
            "kind": self.kind,
            "token": self.token,
            "start": self.start,
            "end": self.end,
        }


# A descriptive name is useful to integrations while ``Finding`` remains a
# short import for tests and route-sweep failure reports.
OpsecFinding = Finding


@dataclass(frozen=True, slots=True)
class _Candidate:
    start: int
    end: int
    kind: str
    # ``None`` means the finding text is ``text[start:end]``, built only for a
    # selected candidate, so overlapping URI candidates hold no copies.
    token: str | None
    priority: int


def _field_path(parent: str, key: object) -> str:
    key_text = str(key)
    if _SAFE_FIELD_KEY_RE.fullmatch(key_text):
        return f"{parent}.{key_text}"
    return f"{parent}[{key_text!r}]"


def _iter_string_leaves(
    value: Any,
    field_path: str,
    seen: set[int] | None = None,
) -> Iterable[tuple[str, str]]:
    """Yield string leaves with stable JSON-like paths.

    API bodies and telemetry are JSON-compatible, but accepting bytes makes
    the helper usable with header adapters without forcing a separate caller.
    Cycles are ignored rather than allowing a malformed test fixture to hang
    a route sweep.
    """
    active = set() if seen is None else seen
    if isinstance(value, str):
        yield field_path, value
        return
    if isinstance(value, (bytes, bytearray)):
        yield field_path, bytes(value).decode("utf-8", errors="replace")
        return
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active:
            return
        next_active = active | {marker}
        for key, child in value.items():
            yield from _iter_string_leaves(child, _field_path(field_path, key), next_active)
        return
    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in active:
            return
        next_active = active | {marker}
        for index, child in enumerate(value):
            yield from _iter_string_leaves(child, f"{field_path}[{index}]", next_active)


def _iter_canaries(canaries: object) -> Iterable[str]:
    if canaries is None:
        return
    if isinstance(canaries, str):
        if canaries:
            yield canaries
        return
    if isinstance(canaries, Mapping):
        for value in canaries.values():
            yield from _iter_canaries(value)
        return
    if isinstance(canaries, Iterable):
        for value in canaries:
            yield from _iter_canaries(value)


def _protected_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return spans for identifiers and syntax that are intentionally safe."""
    spans = [
        (match.start(), match.end())
        for pattern in (_SHA_RE, _RFC3339_RE, _EPIC_RE, _API_ROUTE_RE, _HASH_ROUTE_RE, _ISO_DURATION_RE)
        for match in pattern.finditer(text)
    ]
    if not spans:
        return ()
    spans.sort()
    merged: list[tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _ip_version(candidate: str) -> int | None:
    """Return the address version for a candidate, using ipaddress as parser."""
    value = candidate
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            return None
        value = value[1:closing]

    if "." not in value and ":" not in value:
        return None

    try:
        return ipaddress.ip_address(value).version
    except ValueError:
        pass

    # An IPv4 address with a port is not accepted as one ipaddress token.  Do
    # not parse a host by hand: only the address portion is split, then the
    # address itself is still validated by ipaddress.
    if ":" not in value:
        return None
    address, separator, port = value.rpartition(":")
    if not separator or not port.isdecimal() or not address or len(port) > 5:
        return None
    try:
        return ipaddress.ip_address(address).version
    except ValueError:
        return None


class _Cursor:
    """Index of the last of sorted starts at or before a position, or -1.

    Each scan pass looks positions up in ascending order, so the cursor
    gallops forward from its last answer: a pass of k lookups over n starts
    costs O(k + n) comparisons, not O(k log n).  A lookup behind the cursor
    (the next pass) bisects once.
    """

    def __init__(self, starts: list[int]) -> None:
        self._starts = starts
        self._index = -1

    def locate(self, position: int) -> int:
        starts = self._starts
        low = self._index
        if low >= 0 and starts[low] > position:
            low = -1
        step = 1
        while low + step < len(starts) and starts[low + step] <= position:
            low += step
            step *= 2
        self._index = bisect.bisect_right(starts, position, max(low, 0), min(low + step, len(starts))) - 1
        return self._index


class _SpanSet:
    """Overlap with sorted, disjoint spans."""

    def __init__(self, spans: Iterable[tuple[int, int]]) -> None:
        self._spans = list(spans)
        self._cursor = _Cursor([start for start, _ in self._spans])

    def overlaps(self, start: int, end: int) -> bool:
        """Whether ``[start, end)`` overlaps a span.

        Only the last span starting before ``end`` can: every earlier one ends
        before that one starts.
        """
        index = self._cursor.locate(end - 1)
        return index >= 0 and start < self._spans[index][1]


def _trim_end(text: str, start: int, end: int) -> int:
    """Drop trailing prose punctuation from the whole redaction span."""
    while end > start and text[end - 1] in _PATH_TRAILING_PUNCTUATION:
        end -= 1
    return end


def _valid_authority(text: str, start: int, end: int) -> bool:
    """Accept only RFC 3986 authority syntax before treating a URI as remote."""
    match = _AUTHORITY_RE.fullmatch(text, start, end)
    if match is None or _BAD_ESCAPE_RE.search(text, start, end):
        return False
    literal = match.group("literal")
    if literal is None or _IPVFUTURE_RE.fullmatch(literal):
        return True
    try:
        ipaddress.IPv6Address(literal)
    except ValueError:
        return False
    return True


def _markup_parts(text: str, start: int, end: int) -> Iterable[tuple[int, int]]:
    """Yield prose, inline code contents and Markdown destinations as parts."""
    link_depth = code_ticks = 0
    for match in _MARKUP_RE.finditer(text, start, end):
        marker = match.group()
        if marker.startswith("`"):
            if code_ticks and code_ticks != len(marker):
                continue
            code_ticks = 0 if code_ticks else len(marker)
        elif code_ticks:
            continue
        elif marker == "](" and not link_depth:
            link_depth = 1
        elif link_depth:
            link_depth += -1 if marker == ")" else 1
            if link_depth:
                continue
        else:
            continue
        yield start, match.start()
        start = match.end()
    yield start, end


def _path_root(
    text: str, start: int, end: int, *, check_segments: bool, allow_route: bool, remote: bool,
) -> int | None:
    """Return the first reportable slash in one path component."""
    if start == end:
        return None
    absolute = text[start] == "/"
    dot = _DOT_SEGMENT_RE.search(text, start, end)
    dot_relative = not absolute and dot is not None
    route = allow_route and _API_SEGMENT_RE.match(text, start, end) is not None
    first = start
    if absolute:
        while first < end and text[first] == "/":
            first += 1
        first -= 1
    for root in _ROOT_PREFIX_RE.finditer(text, start, end):
        position = root.start()
        after_dot = dot is not None and position >= dot.end()
        if route and not after_dot:
            continue
        if (dot_relative or after_dot or check_segments) and not _RELATIVE_NAME_RE.match(
            text, root.end(), end
        ):
            return position
        if _BARE_NAME_RE.match(text, root.end(), end):
            continue
        if absolute and position == first and not remote:
            return position
        if position > start:
            previous = text[position - 1]
            if previous == "/" or (previous.isascii() and previous.isalnum()):
                continue
            if dot_relative and previous == "." and any(
                offset >= start and _DOT_SEGMENT_RE.match(text, offset, position + 1)
                for offset in (position - 2, position - 1)
            ):
                continue
            return position
    return None


def _path_root_in_part(
    text: str,
    start: int,
    end: int,
    *,
    decode: bool = True,
    value_context: Literal["value", "api-fragment"] | None = None,
) -> int | None:
    """Lex references and return the first sensitive path offset in the part.

    Split the authority before testing the path. Within a query or fragment,
    delimit values on the raw separators before decoding them once. Invalid
    authorities get no remote-path exemption. Other punctuation can join
    independent references, so the lexer checks both sides.
    """
    api_reference = _API_SEGMENT_RE.match(text, start, end) is not None
    while start < end:
        scheme = _SCHEME_RE.match(text, start, end)
        path_start = scheme.end() if scheme is not None else start
        file_uri = scheme is not None and scheme.group("name").lower() == "file"
        remote = False
        nested_file = None
        has_authority = scheme is not None and text.startswith("//", path_start, end)
        if has_authority:
            authority_start = path_start + 2
            authority_end = _AUTHORITY_END_RE.search(text, authority_start, end)
            path_start = authority_end.start() if authority_end else end
            remote = not file_uri and _valid_authority(
                text, authority_start, path_start
            )
            if (path_start - authority_start == 5 and text[authority_start:path_start].lower() == "file:"
                and text.startswith("//", path_start, end)):
                nested_file = authority_start

        # A file scheme at the cursor has already been consumed. A later
        # zero-width delimiter starts another URI without discarding its scheme.
        separator = _PART_END_RE.search(text, path_start, end)
        part_end = separator.start() if separator else end
        bare_hash = (
            start > 0
            and text[start - 1] == "#"
            and value_context is None
            and (hash_route := _HASH_ROUTE_RE.match(text, start - 1)) is not None
            and hash_route.end() == part_end
        )
        remote = (
            remote
            and _URI_PATH_RE.fullmatch(text, path_start, part_end) is not None
            and _BAD_ESCAPE_RE.search(text, path_start, part_end) is None
        )
        absolute_value = value_context is not None and text.startswith("/", path_start, part_end)
        check_segments = not remote and (absolute_value or value_context == "api-fragment" or (has_authority and not file_uri))
        allow_route = scheme is None and value_context is None

        # A remote path is an RFC 3986 component even when the URL is a query
        # value. Apply the conservative value policy only to host-like paths.
        root = (
            None
            if bare_hash
            else _path_root(
                text,
                path_start,
                part_end,
                check_segments=check_segments,
                allow_route=allow_route,
                remote=remote,
            )
        )
        if (
            decode
            and not remote
            and text.find("%", path_start, root if root is not None else part_end) >= 0
        ):
            # Preserve the authority boundary while examining decoded path data.
            # An encoded value can itself be a reference, so lex it once after
            # decoding. Its raw value is the redaction unit if it contains a root.
            encoded = text[path_start:part_end]
            decoded = unquote(encoded)
            if decoded != encoded:
                if scheme is not None:
                    decoded_root = _path_root(
                        decoded,
                        0,
                        len(decoded),
                        check_segments=check_segments,
                        allow_route=False,
                        remote=False,
                    )
                else:
                    decoded_root = _path_root_in_part(
                        decoded, 0, len(decoded), decode=False, value_context=value_context
                    )
                if decoded_root is not None and (
                    root is None or decoded_root < len(unquote(text[path_start:root]))
                ):
                    return scheme.start("name") if file_uri and scheme is not None else path_start
        if root is not None:
            if (
                file_uri
                and scheme is not None
                and text.count("/", path_start, root) == root - path_start
            ):
                return scheme.start("name")
            return root
        if nested_file is not None:
            start = nested_file
            continue
        if separator is None:
            break
        delimiter = separator.group()
        if delimiter == "#" and api_reference:
            value_context = "api-fragment"
        elif delimiter in ("?", "#") and value_context is None and (scheme is not None or part_end > start):
            value_context = "value"
        start = separator.end()
    return None


def _filesystem_candidates(text: str) -> Iterable[_Candidate]:
    # Path candidates in the same run all cover the tail. Selection would
    # keep only the first one, so there is no need to materialize the others.
    for run in _PATH_RUN_RE.finditer(text):
        end = _trim_end(text, run.start(), run.end())
        for part_start, part_end in _markup_parts(text, run.start(), run.end()):
            start = _path_root_in_part(text, part_start, min(part_end, end))
            if start is not None:
                yield _Candidate(start, end, "filesystem-root", None, 80)
                break


def _is_css_numeric_property(text: str, match: re.Match[str]) -> bool:
    """Avoid treating CSS declarations as hostname/port values."""
    if match.group("host").lower() not in _CSS_NUMERIC_PROPERTIES:
        return False
    prefix = text[max(0, match.start() - 256) : match.start()]
    if prefix.rfind("{") > prefix.rfind("}"):
        return True
    return re.search(r"\bstyle\s*=", prefix, re.IGNORECASE) is not None


def _select_candidates(candidates: Iterable[_Candidate]) -> list[_Candidate]:
    """Deduplicate overlapping descriptions while retaining the strongest one.

    The higher priority wins, then the earlier start, then the longer span.
    Priorities are a handful of fixed tiers: within a tier candidates arrive
    in start order, so only the last one taken can overlap. Spans taken by
    higher tiers are disjoint and sorted, so one forward pass finds overlaps.
    """
    unique = {(item.start, item.end, item.kind, item.token): item for item in candidates}
    tiers: dict[int, list[_Candidate]] = {}
    for item in unique.values():
        tiers.setdefault(item.priority, []).append(item)
    selected: list[_Candidate] = []
    for priority in sorted(tiers, reverse=True):
        index = 0
        taken: list[_Candidate] = []
        for candidate in sorted(tiers[priority], key=lambda item: (item.start, -(item.end - item.start), item.kind)):
            if taken and candidate.start < taken[-1].end:
                continue
            while index < len(selected) and selected[index].end <= candidate.start:
                index += 1
            if index < len(selected) and selected[index].start < candidate.end:
                continue
            taken.append(candidate)
        selected = sorted(selected + taken, key=lambda item: item.start)
    return sorted(selected, key=lambda item: (item.start, item.end, item.kind))


def scan_text(
    text: str,
    *,
    operation: str = DEFAULT_OPERATION,
    field_path: str = "body",
    canaries: object = (),
) -> list[Finding]:
    """Scan one text value and preserve its operation/field provenance.

    A filesystem finding starts at the bounded root, the scheme of a file URI
    whose first path segment is a bounded root, or the raw start of a sensitive
    encoded component. Its span covers the rest of the whitespace-, quote- or
    angle-bracket-delimited run, less trailing prose punctuation and backticks.
    The sanitizer replaces this span, which may include a glued neighbour.

    The lexer separates Markdown and code, URI authorities and paths, and
    query and fragment values before checking for roots. A dot-relative path
    still reports whole bounded-root segments. A malformed reference keeps
    the local path checks, and a decoded value is redacted in its raw form.
    """
    if not isinstance(text, str):
        return []

    protected = _SpanSet(_protected_spans(text))
    candidates: list[_Candidate] = []

    for canary in dict.fromkeys(_iter_canaries(canaries)):
        for match in re.finditer(re.escape(canary), text):
            candidates.append(_Candidate(match.start(), match.end(), "canary", match.group(0), 100))

    candidates.extend(_filesystem_candidates(text))

    for match in _USER_AT_HOST_RE.finditer(text):
        host = match.group("token").rsplit("@", 1)[-1].split(":", 1)[0]
        if not host.isdigit() and not protected.overlaps(match.start(), match.end()):
            candidates.append(_Candidate(match.start(), match.end(), "user-at-host", match.group(0), 75))

    for match in _IP_CANDIDATE_RE.finditer(text):
        raw = match.group(0)
        candidate = raw if raw.startswith("[") else raw.rstrip(".")
        if not candidate or protected.overlaps(match.start(), match.start() + len(candidate)):
            continue
        version = _ip_version(candidate)
        if version is not None:
            candidates.append(
                _Candidate(
                    match.start(),
                    match.start() + len(candidate),
                    f"ipv{version}",
                    candidate,
                    70,
                )
            )

    for match in _HOST_PORT_RE.finditer(text):
        if (
            not _is_css_numeric_property(text, match)
            and 0 < int(match.group("port")) <= 65535
            and not protected.overlaps(match.start(), match.end())
        ):
            candidates.append(_Candidate(match.start(), match.end(), "host-port", match.group(0), 60))

    for match in _SSH_ALIAS_RE.finditer(text):
        alias = match.group("alias").rstrip(".")
        start = match.start("alias")
        end = match.end("alias")
        if alias.lower() not in _SSH_PROSE_WORDS and not protected.overlaps(start, end):
            candidates.append(_Candidate(start, end, "ssh-alias", alias, 50))

    for match in _TRANSCRIPT_FILENAME_RE.finditer(text):
        if not protected.overlaps(match.start(), match.end()):
            candidates.append(_Candidate(match.start(), match.end(), "transcript-filename", match.group(0), 40))

    # A planted canary is reported in addition to what it overlaps; it never
    # hides a real finding (the canary marks a sweep fixture, not a leak class).
    selected = sorted(
        _select_candidates([item for item in candidates if item.kind != "canary"])
        + _select_candidates([item for item in candidates if item.kind == "canary"]),
        key=lambda item: (item.start, item.end, item.kind),
    )
    return [
        Finding(
            operation=str(operation),
            field_path=field_path,
            kind=candidate.kind,
            token=candidate.token if candidate.token is not None else text[candidate.start : candidate.end],
            start=candidate.start,
            end=candidate.end,
        )
        for candidate in selected
    ]


def scan_value(
    value: Any,
    *,
    operation: str = DEFAULT_OPERATION,
    field_path: str = "body",
    canaries: object = (),
) -> list[Finding]:
    """Recursively scan JSON-compatible values, retaining each leaf path."""
    findings: list[Finding] = []
    for leaf_path, text in _iter_string_leaves(value, field_path):
        findings.extend(
            scan_text(
                text,
                operation=operation,
                field_path=leaf_path,
                canaries=canaries,
            )
        )
    return findings


def scan_body(
    body: Any,
    *,
    operation: str = DEFAULT_OPERATION,
    canaries: object = (),
) -> list[Finding]:
    """Scan an API response body under the ``body`` provenance root."""
    return scan_value(body, operation=operation, field_path=_BODY_PATH, canaries=canaries)


def scan_headers(
    headers: Any,
    *,
    operation: str = DEFAULT_OPERATION,
    canaries: object = (),
) -> list[Finding]:
    """Scan every response-header value under the ``headers`` root."""
    return scan_value(headers, operation=operation, field_path=_HEADERS_PATH, canaries=canaries)


def scan_telemetry(
    telemetry: Any,
    *,
    operation: str = DEFAULT_OPERATION,
    canaries: object = (),
) -> list[Finding]:
    """Scan the optional ``_telemetry`` section under its own root."""
    return scan_value(telemetry, operation=operation, field_path=_TELEMETRY_PATH, canaries=canaries)


def scan_response(
    operation: str = DEFAULT_OPERATION,
    body: Any = _MISSING,
    headers: Any = None,
    telemetry: Any = _MISSING,
    *,
    canaries: object = (),
) -> list[Finding]:
    """Scan body, headers, and telemetry as separate response sections.

    ``body`` may contain a top-level ``_telemetry`` mapping when the caller
    has not supplied ``telemetry`` separately.  It is moved to the dedicated
    provenance root rather than reported as an ordinary body field.
    """
    response_body = None if body is _MISSING else body
    response_telemetry = telemetry
    if isinstance(response_body, Mapping) and _TELEMETRY_PATH in response_body:
        embedded_telemetry = response_body[_TELEMETRY_PATH]
        response_body = {key: value for key, value in response_body.items() if key != _TELEMETRY_PATH}
        if response_telemetry is _MISSING:
            response_telemetry = embedded_telemetry

    findings = scan_body(response_body, operation=operation, canaries=canaries)
    findings.extend(scan_headers(headers, operation=operation, canaries=canaries))
    if response_telemetry is not _MISSING:
        findings.extend(scan_telemetry(response_telemetry, operation=operation, canaries=canaries))
    return findings


def scan_response_fields(
    body: Any = None,
    headers: Any = None,
    telemetry: Any = _MISSING,
    *,
    operation: str = DEFAULT_OPERATION,
    canaries: object = (),
) -> list[Finding]:
    """Body-first wrapper for callers that assemble response fields positionally."""
    return scan_response(operation, body, headers, telemetry, canaries=canaries)


scan_api_response = scan_response
scan = scan_response


__all__ = [
    "BOUNDED_FILESYSTEM_ROOTS",
    "DEFAULT_OPERATION",
    "Finding",
    "OpsecFinding",
    "scan",
    "scan_api_response",
    "scan_body",
    "scan_headers",
    "scan_response",
    "scan_response_fields",
    "scan_telemetry",
    "scan_text",
    "scan_value",
]
