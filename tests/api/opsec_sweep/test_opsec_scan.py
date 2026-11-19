"""Positive and negative corpus for the shared Monitor OPSEC scanner."""

from __future__ import annotations

import pytest

from lu_machinery.api.opsec_scan import scan_response, scan_text


@pytest.mark.parametrize(
    "text",
    [
        "UnknownError Unexpected server error; retrying",
        "See https://docs.example.org/guide?part=2 for help",
        "Retry scripts/api/lane_health.py after ENOTFOUND",
        "ENOTFOUND scripts/api/lane_health.py",
        "open tests/fixtures/home/example.json",
        r"open tests\fixtures\Users\example.json",
        "relative docs/~/.ssh/example",
        "codes: ENOTFOUND EAI_AGAIN EAI_NONAME ENODATA",
        "Budget ~500K/1M, fraction ~2/3, split ~50/50, cost ~35/module",
    ],
)
def test_scan_text_keeps_public_urls_repository_paths_and_ordinary_diagnostics(text):
    assert scan_text(text) == []


def _ipv4(*octets: int) -> str:
    return ".".join(str(octet) for octet in octets)


def _ipv6() -> str:
    return ":".join(("2001", "db8", "0", "0", "0", "0", "0", "17"))  # noqa: FLY002 - synthetic address


def test_positive_corpus_catches_each_r3_shape() -> None:
    canary = "fixture-opsec-host-synthetic"
    ipv4 = _ipv4(10, 42, 0, 7)
    ipv6 = _ipv6()
    positive = (
        (ipv4, "ipv4"),
        (f"{ipv4}:8443", "ipv4"),
        (f"[{ipv6}]:9443", "ipv6"),
        ("/home/fixture/repository", "filesystem-root"),
        ("/Users/fixture/.claude/projects/session.jsonl", "filesystem-root"),
        ("/Volumes/fixture/store.sqlite", "filesystem-root"),
        ("/private/var/folders/fixture", "filesystem-root"),
        ("/opt/monitor/config", "filesystem-root"),
        ("/srv/monitor/data", "filesystem-root"),
        ("/tmp/fixture", "filesystem-root"),
        ("/var/lib/monitor", "filesystem-root"),
        ("runner@monitor-alias", "user-at-host"),
        ("ssh monitor-alias", "ssh-alias"),
        ("monitor-alias:8765", "host-port"),
        ("session-synthetic.jsonl", "transcript-filename"),
        (canary, "canary"),
    )

    for text, kind in positive:
        findings = scan_text(text, operation="GET /api/opsec", field_path="body.value", canaries=(canary,))
        assert any(finding.kind == kind and finding.token in text for finding in findings), (text, findings)


def test_negative_corpus_exempts_identifiers_timestamps_routes_and_durations() -> None:
    sha1 = "0123456789abcdef" * 2 + "01234567"
    sha256 = "abcdef0123456789" * 4
    negative = (
        sha1,
        f"sha256:{sha256}",
        "2026-08-24T19:12:43Z",
        "2026-08-24T19:12:43.123+02:00",
        "epic:9999",
        "/api/session-streams/v1/health",
        "timeout=PT1H30M",
        "retry-after=P2DT4H",
        "version 1.2.3",
        'href="#/home"',
        "location.hash = '#/home';",
        'parseRoute(location.hash || "#/home")',
    )

    for text in negative:
        assert scan_text(text, operation="GET /api/opsec", field_path="body.value") == [], text


def test_response_scan_keeps_body_header_and_telemetry_provenance() -> None:
    canary = "planted-route-sweep-canary"
    findings = scan_response(
        "GET /api/health",
        body={"nested": {"repo_root": "/tmp/fixture-root"}, "canary": canary},
        headers={"X-Forwarded-Host": "monitor-alias:8765"},
        telemetry={"transcript": "session-synthetic.jsonl"},
        canaries=(canary,),
    )

    assert [(finding.operation, finding.field_path) for finding in findings] == [
        ("GET /api/health", "body.nested.repo_root"),
        ("GET /api/health", "body.canary"),
        ("GET /api/health", "headers.X-Forwarded-Host"),
        ("GET /api/health", "_telemetry.transcript"),
    ]
    assert {finding.kind for finding in findings} == {"canary", "filesystem-root", "host-port", "transcript-filename"}


def test_response_scan_extracts_embedded_telemetry_as_a_separate_section() -> None:
    findings = scan_response(
        "GET /api/state",
        body={"ok": True, "_telemetry": {"newest_transcript": "current.jsonl"}},
    )

    assert [(finding.field_path, finding.token) for finding in findings] == [
        ("_telemetry.newest_transcript", "current.jsonl")
    ]


def test_ip_parser_does_not_treat_colons_in_timestamps_or_durations_as_addresses() -> None:
    text = "created=2026-08-24T19:12:43Z; timeout=PT30S; route=/api/health"
    assert scan_text(text, operation="GET /api/health", field_path="body") == []


def test_host_port_scanner_ignores_css_but_keeps_bare_hostnames() -> None:
    assert scan_text(
        '<style>.card { font-size:13; }</style>',
        operation="dashboard:synthetic.html",
    ) == []
    findings = scan_text("width:8765", operation="GET /api/health", field_path="body")
    assert [(finding.kind, finding.token) for finding in findings] == [("host-port", "width:8765")]


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("read file:///home/fixture-user/notes.md.", "file:///home/fixture-user/notes.md"),
        ("FILE:///Users/fixture-user/notes.md", "FILE:///Users/fixture-user/notes.md"),
        ("file://fixture-host/home/fixture-user/x", "file://fixture-host/home/fixture-user/x"),
        ("file:////home/fixture-user/x", "file:////home/fixture-user/x"),
        ("file:/home/fixture-user/x", "file:/home/fixture-user/x"),
        ("/home/fixture-user/" + "ab" * 20 + "/x", "/home/fixture-user/" + "ab" * 20 + "/x"),
        ("/home/fixture-user/run-2026-08-24T19:12:43Z/x", "/home/fixture-user/run-2026-08-24T19:12:43Z/x"),
        ("file://api/home/fixture-user/input", "file://api/home/fixture-user/input"),
        ("/api/fixture?source=file:///home/fixture-user/input", "file:///home/fixture-user/input"),
        ("...file:///home/fixture-user/x", "file:///home/fixture-user/x"),
        ("+FILE://fixture-host/home/fixture-user/x", "FILE://fixture-host/home/fixture-user/x"),
        ("file:///etc/fixture.conf,file://fixture-host/home/fixture-user/x", "file://fixture-host/home/fixture-user/x"),
        ("file:///etc/fixture.conf?u=file:///home/fixture-user/x", "file:///home/fixture-user/x"),
        ("[fixture](file://fixture-host./home)", "file://fixture-host./home"),
        ("Read `file://fixture-host/home`.", "file://fixture-host/home"),
        # Fail-closed span contract: a glued neighbour is covered, never path bytes left behind.
        ("[local](file:///home/fixture-user/x),[docs](/api/health)", "file:///home/fixture-user/x),[docs](/api/health"),
        ("/api/fixture?source=/home/fixture-user/x&view=page#/details", "/home/fixture-user/x&view=page#/details"),
        ("/home/fixture-user/team,notes/x", "/home/fixture-user/team,notes/x"),
        ("[a](file:/home),[b](/api/health)", "file:/home),[b](/api/health"),
        # Fail-closed detection: a root followed by a non-name character counts, as in the bare rule.
        ("file:///tmp.backup/fixture.conf", "file:///tmp.backup/fixture.conf"),
        ("file:///tmp,backup/fixture.conf", "file:///tmp,backup/fixture.conf"),
        ("file:///tmp.#anchor", "file:///tmp.#anchor"),
        ("/api/fixture?source=file:///etc/fixture.conf,file:/tmp.backup/x", "file:/tmp.backup/x"),
        ("/api/fixture?source=/tmp.backup/fixture.conf", "/tmp.backup/fixture.conf"),
        ("/api/fixture/{id}/home/view?source=/home/fixture-user/data", "/home/fixture-user/data"),
    ],
)
def test_home_path_file_uris_and_identifier_tails_are_flagged(text: str, token: str) -> None:
    """Defect class: a home path hidden by its file-URI shape or a protected span in it (#909)."""
    findings = scan_text(text, operation="GET /api/opsec", field_path="body.value")
    assert [(finding.kind, finding.token) for finding in findings] == [("filesystem-root", token)], text


def test_planted_canary_never_hides_a_file_uri_finding() -> None:
    """Defect class: a canary inside a widened path span displaced the path finding (#909)."""
    findings = scan_text("file:///home#fixture-canary", canaries=("fixture-canary",))
    assert [(finding.kind, finding.token) for finding in findings] == [
        ("filesystem-root", "file:///home#fixture-canary"),
        ("canary", "fixture-canary"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "see docs/home/fixture-user/readme.md",
        "file:docs/home/fixture-user/readme.md",
        "https://tmp.example.org/home/page",
        "file:///etc/fixture.conf",
        "file://home.example.invalid/etc/fixture.conf",
        "file://tmp/etc/fixture.conf",
        "profile://fixture-host/home/fixture-user/input",
        "`file://fixture-host`+`docs/home/guide.md`",
        "/api/fixture/{account}/home/view",
    ],
)
def test_relative_paths_and_url_authorities_stay_unflagged(text: str) -> None:
    assert scan_text(text, operation="GET /api/opsec", field_path="body.value") == [], text


# Designated amendment (2026-11-19): a dot-relative path, or an /api query or
# fragment value, whose path holds a whole bounded-root segment is flagged
# (fail closed; over-flagging there is accepted).
@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("./home/fixture-user/x", "/home/fixture-user/x"),
        ("../home/fixture-user/x", "/home/fixture-user/x"),
        ("../api/../home/fixture-user/x", "/home/fixture-user/x"),
        # #924 retiered this #920 negative under the dot-relative contract.
        ("../api/../home/guide.md", "/home/guide.md"),
        ("../api/home/fixture-user/x", "/home/fixture-user/x"),
        ("../api/fixture/{id}/home/view", "/home/view"),
        ("docs/../home/fixture-user/x", "/home/fixture-user/x"),
        (".//home/fixture-user/x", "/home/fixture-user/x"),
        ("../Users/fixture-user/x", "/Users/fixture-user/x"),
        ("./opt/fixture.log", "/opt/fixture.log"),
        ("see [notes](../home/fixture-user/x).", "/home/fixture-user/x"),
        ("cd ../home.", "/home"),
        ("/api/fixture?next=../home/fixture-user/x", "/home/fixture-user/x"),
        ("/api/fixture#../api/../home/fixture-user/x", "/home/fixture-user/x"),
        ("/api/fixture?next=/api/fixture/{id}/home/view", "/home/view"),
        ("/api/fixture?next=../api/home/view&page=2", "/home/view&page=2"),
        # An absolute path keeps the bare boundary; a dot segment ends the route exemption.
        ("/api/../home/fixture-user/x", "/home/fixture-user/x"),
        ("/api/fixture/../../home.old/fixture-user", "/home.old/fixture-user"),
        # A dot that ends a longer segment name opens no dot-relative path.
        ("\u0434\u0438\u0432./home.old/fixture-user", "/home.old/fixture-user"),
        # A bare /api route opens its query or fragment too.
        ("/api?next=/api/fixture/{id}/home/view", "/home/view"),
        ("/api#docs/home/fixture-user", "/home/fixture-user"),
        # After a dot segment an absolute path is judged by segments, repeated slashes included.
        ("/api/..//home/fixture-user", "/home/fixture-user"),
        ("/api/../{id}/home/fixture-user", "/home/fixture-user"),
        ("/api/./{id}/home/fixture-user", "/home/fixture-user"),
        ("/fixture/..//home/fixture-user", "/home/fixture-user"),
        # A trailing dot run is prose punctuation, not part of the name.
        ("cd ../home..", "/home"),
        # Accepted over-flagging: any whole root segment of a dot-segment path.
        ("./docs/home/fixture-user", "/home/fixture-user"),
        ("/api/../docs/home/fixture-user", "/home/fixture-user"),
    ],
)
def test_dot_relative_and_api_query_root_segments_are_flagged(text: str, token: str) -> None:
    """Defect class: a home-root segment hidden by a relative or nested-route shape (#909)."""
    findings = scan_text(text, operation="GET /api/opsec", field_path="body.value")
    assert [(finding.kind, finding.token) for finding in findings] == [("filesystem-root", token)], text


@pytest.mark.parametrize(
    "text",
    [
        "./docs/x",
        "../guide.md",
        "../api/v1/x",
        "[a](../docs/x) [b](./guide.md)",
        "../homework/x",
        "./home-page/x",
        "../tmpl/x",
        # A root name with an extension is a file name, not a root segment.
        "../home.md",
        "./tmp.log",
        "../Users.md",
        "docs/../home.md",
        "/api/fixture?next=../guide.md",
        "/api/fixture?next=../api/v1/x",
        "/api/fixture#./docs/x",
        "/api/fixture?next=../home.md",
        # A name character or a dot run plus one continues the name.
        "../docs/home\u00e9/x",
        "./docs/home+page/x",
        "../docs/home~page/x",
        "../docs/home..md",
        "/api?next=docs/home+page/x",
        # A value that starts with a root name names no root.
        "/api/fixture?tab=home",
        "/api/fixture?path=home/fixture-user",
        "/api?tab=home",
    ],
)
def test_ordinary_relative_paths_stay_unflagged(text: str) -> None:
    """Defect class: an ordinary relative path reported as a host path (#909)."""
    assert scan_text(text, operation="GET /api/opsec", field_path="body.value") == [], text


@pytest.mark.parametrize(
    ("text", "token"),
    [
        pytest.param("/api?url=https://tmp/x", None, id="query-authority"),
        pytest.param("/api#https://fixture.example/home/page", None, id="fragment-remote-path"),
        pytest.param("/api?url=https://tmp/page&doc=../guide.md", None, id="independent-query-values"),
        pytest.param("/api?url=https://fixture.example&path=/home/fixture/x", "/home/fixture/x", id="authority-only-query-value"),
        pytest.param("/api?url=https://fixture.example&path=../home/fixture/x", "/home/fixture/x", id="authority-only-dot-relative-value"),
        pytest.param("/api?url=https://fixture.example&path=%2Fhome%2Fx", "%2Fhome%2Fx", id="authority-only-encoded-value"),
        pytest.param("/api?url=https://fixture.example&next=file:///home/fixture/x", "file:///home/fixture/x", id="authority-only-file-value"),
        pytest.param("/api?url=https://fixture.example=/home/fixture/x", "/home/fixture/x", id="authority-only-assignment-boundary"),
        pytest.param("/api#https://fixture.example&path=/tmp/fixture/x", "/tmp/fixture/x", id="authority-only-fragment-value"),
        pytest.param("/api?url=https://fixture.example&/tmp/fixture/x", "/tmp/fixture/x", id="authority-only-unnamed-param"),
        pytest.param("https://fixture.example,/tmp/fixture/x", "/tmp/fixture/x", id="authority-only-comma-join"),
        pytest.param("https://fixture.example;/home/fixture/x", "/home/fixture/x", id="authority-only-semicolon-join"),
        pytest.param("https://fixture.example,../home/fixture/x", "/home/fixture/x", id="authority-only-dot-relative-join"),
        pytest.param("https://fixture.example+file:///home/fixture/x", "file:///home/fixture/x", id="authority-only-file-join"),
        pytest.param("https://fixture.example;other:/home/fixture/x", "/home/fixture/x", id="authority-joined-other-scheme"),
        pytest.param("https://fixture.example)/home/fixture/x", "/home/fixture/x", id="authority-only-prose-closer"),
        pytest.param("https://fixture.example!/home/fixture/x", "/home/fixture/x", id="authority-only-prose-punctuation"),
        pytest.param("https://fixture.example:/home/fixture/x", "/home/fixture/x", id="authority-only-empty-port"),
        pytest.param("/api?url=https%3A%2F%2Ffixture.example%26p%3D%2Fhome%2Fx", "https%3A%2F%2Ffixture.example%26p%3D%2Fhome%2Fx", id="encoded-authority-only-boundary"),
        pytest.param("/api?doc=examples/home/page", None, id="ordinary-relative-query-value"),
        pytest.param("?next=/api/fixture/{id}/home/view", "/home/view", id="query-only-reference"),
        pytest.param("`./docs`+`examples/home/page`", None, id="independent-code-spans"),
        pytest.param("[a](./docs)[b](examples/home/page)", None, id="independent-link-destinations"),
        pytest.param("[a](https://fixture.example/home/(notes))", None, id="balanced-link-destination"),
        pytest.param("/api/[a](/home/fixture/x)", "/home/fixture/x", id="link-after-api-route"),
        pytest.param(r"[a](../docs/fixture\)/name/home/demo)", "/home/demo", id="escaped-destination-close"),
        pytest.param("./docs/[a](/samples/home/demo", "/home/demo", id="unclosed-link-destination"),
        pytest.param("./docs/x](/samples/home/demo)", "/home/demo", id="missing-link-label"),
        pytest.param("/api?uri=https://[bad]/api/items/home/fixture", "/home/fixture", id="malformed-authority"),
        pytest.param("https://fixture.example/home/path%xy", "/home/path%xy", id="malformed-uri-escape"),
        pytest.param("file://file://fixture/home/x", "file://fixture/home/x", id="adjacent-uri-authority"),
        pytest.param("[path](file://fixture(backup)/home)", "file://fixture(backup)/home", id="uri-authority-subdelim"),
        pytest.param("file://fixture=scope/home/fixture/x", "file://fixture=scope/home/fixture/x", id="file-authority-equals"),
        pytest.param("file://fixture&scope/home/fixture/x", "file://fixture&scope/home/fixture/x", id="file-authority-ampersand"),
        pytest.param("/api?file=file://fixture&path=%2Fhome%2Fx", "%2Fhome%2Fx", id="file-authority-joined-encoded-value"),
        pytest.param("file://fixture,../samples/home/fixture/x", "/home/fixture/x", id="file-authority-joined-dot-path"),
        pytest.param("https://fixture.example!../samples/home/fixture/x", "/home/fixture/x", id="remote-authority-dot-path-after-prose-punctuation"),
        pytest.param("./docs/section;%78/home/fixture", "/home/fixture", id="relative-path-subdelim"),
        pytest.param("../docs/name./home.md", None, id="relative-dotted-filename"),
        pytest.param("/api?source=%2Fhome%2Ffixture%2Fx&view=1", "%2Fhome%2Ffixture%2Fx&view=1", id="encoded-query"),
        pytest.param("file://fixture/%55sers/fixture/x", "file://fixture/%55sers/fixture/x", id="encoded-file-path"),
        pytest.param("/api?url=%66ile%3A%2F%2F%2fhome%2Fx", "%66ile%3A%2F%2F%2fhome%2Fx", id="encoded-file-uri"),
        pytest.param("/api#%2ftmp%2Fx", "%2ftmp%2Fx", id="encoded-fragment"),
        pytest.param("/api?url=https%3A%2F%2Ftmp%2Fhome%2Fpage", None, id="encoded-remote-uri"),
        pytest.param("/api?url=https://tmp/home/%20note", None, id="encoded-remote-path"),
        pytest.param("/api?x=..%2Fhome.md", None, id="encoded-relative-filename"),
        pytest.param("https://fixture.example/api?x=/home/fixture", "/home/fixture", id="remote-query-local-path"),
    ],
)
def test_path_detection_respects_parsed_parts(text: str, token: str | None) -> None:
    """Defect class: another part's syntax hid a local path or flagged a public reference (#922)."""
    paths = [finding for finding in scan_text(text) if finding.kind == "filesystem-root"]
    assert [finding.token for finding in paths] == ([] if token is None else [token])
    assert all(text[finding.start : finding.end] == finding.token for finding in paths)


@pytest.mark.parametrize(
    "text",
    [
        "/home/fixture/repository",
        "#/home/fixture/repository",
        "href=\"#/home/fixture\"",
        "#/home/../fixture",
    ],
)
def test_hash_route_exemption_does_not_hide_filesystem_paths(text: str) -> None:
    findings = scan_text(text, operation="GET /api/opsec", field_path="body.value")
    assert [finding.kind for finding in findings] == ["filesystem-root"], text
