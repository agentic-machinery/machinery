"""Unit tests for scripts/utils/claude_version.py — version gate helper.

Covers:
- Semver parsing edge cases (bare, noisy output, v-prefix, invalid)
- Support detection (true, false old version, false on exception)
- Cache behavior (scoped by prefix, hits on repeat)

Issue: #1179
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from jsonschema import validate

from lu_machinery.docs.docs_inventory import build, canonical, digest, main, metadata
from lu_machinery.utils.claude_version import (
    _parse_claude_semver,
    _reset_cache_for_tests,
    remember_support,
    supports_exclude_dynamic_system_prompt_sections,
)
from tests.helpers.product_tree import configured_roots as configured_roots


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the per-prefix cache before every test to prevent cross-test leaks."""
    _reset_cache_for_tests()
    yield
    _reset_cache_for_tests()


# ---------------------------------------------------------------------------
# _parse_claude_semver — pure function, no subprocess
# ---------------------------------------------------------------------------

def test_parse_semver_basic():
    """Bare 'X.Y.Z' string parses."""
    assert _parse_claude_semver("2.1.98") == (2, 1, 98)


def test_parse_semver_with_noise():
    """Version surrounded by parenthetical noise still parses."""
    assert _parse_claude_semver("2.1.98 (Claude Code)") == (2, 1, 98)
    assert _parse_claude_semver("Claude Code v2.1.98 (build abc123)") == (2, 1, 98)


def test_parse_semver_invalid():
    """Unparsable input returns None, not a crash."""
    assert _parse_claude_semver("") is None
    assert _parse_claude_semver("not a version") is None
    assert _parse_claude_semver("1.2") is None  # too few parts
    assert _parse_claude_semver("abc.def.ghi") is None


def test_parse_semver_rejects_four_part_version():
    """Four-part version strings must NOT match as 3-part semver — the
    regex enforces word boundaries so 4-segment strings are not a valid 3-part semver.
    This prevents ambiguous parses."""
    four_part = f"{2}.{1}.{98}.{1}"
    assert _parse_claude_semver(four_part) is None


def test_parse_semver_skips_npm_notice():
    """Gemini finding #2: npm update notices must NOT fool the parser.

    The line 'npm notice New minor version of npm available! 10.2.4 -> 10.8.1'
    contains valid-looking semvers but is not the Claude Code version.
    """
    noisy = (
        "npm notice New minor version of npm available! 10.2.4 -> 10.8.1\n"
        "2.1.98 (Claude Code)\n"
    )
    assert _parse_claude_semver(noisy) == (2, 1, 98)


# ---------------------------------------------------------------------------
# Gemini finding #1: combine stdout + stderr explicitly
# ---------------------------------------------------------------------------

def test_supports_reads_stderr_when_stdout_is_just_newline():
    """Gemini finding #1: if stdout is '\\n' (truthy but blank), we must
    still check stderr. Previous implementation with 'result.stdout or
    result.stderr' would short-circuit because '\\n' is truthy.
    """
    with patch(
        "lu_machinery.utils.claude_version.subprocess.run",
        return_value=_mock_run(stdout="\n", stderr="2.1.98"),
    ):
        assert supports_exclude_dynamic_system_prompt_sections(["claude"]) is True


# ---------------------------------------------------------------------------
# Gemini finding #3: bare string defensive handling
# ---------------------------------------------------------------------------

def test_supports_handles_bare_string_prefix():
    """Gemini finding #3: passing a bare string 'claude' instead of ['claude']
    must NOT unpack into individual characters and try to exec binary 'c'.
    """
    with patch(
        "lu_machinery.utils.claude_version.subprocess.run",
        return_value=_mock_run(stdout="2.1.98"),
    ) as mock:
        result = supports_exclude_dynamic_system_prompt_sections("claude")
        assert result is True
        # Verify subprocess was called with ["claude", "--version"] not
        # ["c", "l", "a", "u", "d", "e", "--version"]
        call_args = mock.call_args[0][0]
        assert call_args[0] == "claude"
        assert call_args[-1] == "--version"
        assert len(call_args) == 2


# ---------------------------------------------------------------------------
# Gemini residual risk: no caching of transient failures
# ---------------------------------------------------------------------------

def test_timeout_failure_not_cached():
    """Gemini residual: TimeoutExpired must NOT be cached permanently.

    A slow npx probe on first call should not disable the optimization
    for the lifetime of the process. A later call should be able to retry.
    """
    timeout_mock = MagicMock(side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5))
    with patch("lu_machinery.utils.claude_version.subprocess.run", timeout_mock):
        result1 = supports_exclude_dynamic_system_prompt_sections(["claude"])
        assert result1 is False

    # Now the network is back. A retry should re-probe, not return the
    # cached False from the timeout.
    success_mock = MagicMock(return_value=_mock_run(stdout="2.1.98"))
    with patch("lu_machinery.utils.claude_version.subprocess.run", success_mock):
        result2 = supports_exclude_dynamic_system_prompt_sections(["claude"])
        assert result2 is True, "timeout should not poison cache permanently"
        assert success_mock.call_count == 1


def test_file_not_found_is_cached():
    """FileNotFoundError (binary genuinely missing) IS a definitive outcome
    and should be cached to avoid re-probing repeatedly."""
    missing_mock = MagicMock(side_effect=FileNotFoundError("claude not found"))
    with patch("lu_machinery.utils.claude_version.subprocess.run", missing_mock):
        supports_exclude_dynamic_system_prompt_sections(["claude"])
        supports_exclude_dynamic_system_prompt_sections(["claude"])
    assert missing_mock.call_count == 1, "FileNotFoundError should be cached"


# ---------------------------------------------------------------------------
# supports_exclude_dynamic_system_prompt_sections — subprocess-backed
# ---------------------------------------------------------------------------

def _mock_run(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Build a MagicMock that mimics subprocess.CompletedProcess."""
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


def test_supports_true_on_minimum_version():
    """Exactly 2.1.98 returns True."""
    with patch("lu_machinery.utils.claude_version.subprocess.run", return_value=_mock_run(stdout="2.1.98")):
        assert supports_exclude_dynamic_system_prompt_sections(["claude"]) is True


def test_supports_false_old_version():
    """Versions < 2.1.98 return False (no flag appended)."""
    with patch("lu_machinery.utils.claude_version.subprocess.run", return_value=_mock_run(stdout="2.1.97")):
        assert supports_exclude_dynamic_system_prompt_sections(["claude"]) is False


def test_supports_false_exception():
    """Any exception from subprocess.run falls back to False safely."""
    with patch("lu_machinery.utils.claude_version.subprocess.run", side_effect=FileNotFoundError("claude not found")):
        assert supports_exclude_dynamic_system_prompt_sections(["claude"]) is False


def test_supports_false_unparsable_output():
    """Version output that can't be parsed falls back to False."""
    with patch("lu_machinery.utils.claude_version.subprocess.run", return_value=_mock_run(stdout="garbage output")):
        assert supports_exclude_dynamic_system_prompt_sections(["claude"]) is False


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------

def test_cache_hits_on_repeat():
    """Second call with same prefix does NOT re-invoke subprocess.run."""
    mock = MagicMock(return_value=_mock_run(stdout="2.1.98"))
    with patch("lu_machinery.utils.claude_version.subprocess.run", mock):
        supports_exclude_dynamic_system_prompt_sections(["claude"])
        supports_exclude_dynamic_system_prompt_sections(["claude"])
        supports_exclude_dynamic_system_prompt_sections(["claude"])
    assert mock.call_count == 1, "cache should have prevented duplicate probes"


def test_cache_scoped_by_prefix():
    """Different prefixes get independent cache entries; multi-word npx prefixes are not probed."""
    mock = MagicMock(return_value=_mock_run(stdout="2.1.98"))
    with patch("lu_machinery.utils.claude_version.subprocess.run", mock):
        assert supports_exclude_dynamic_system_prompt_sections(["claude"]) is True
        assert supports_exclude_dynamic_system_prompt_sections(["/opt/claude/bin/claude"]) is True
        assert supports_exclude_dynamic_system_prompt_sections(["/usr/local/bin/claude"]) is True
        assert supports_exclude_dynamic_system_prompt_sections(["npx", "@anthropic-ai/claude-code@latest"]) is False
    assert mock.call_count == 3, "each unique single-word prefix should be probed once; npx prefix is not probed"


def test_remember_support_overwrites_cached_verdict():
    """remember_support overwrites a cached entry (tests simulate old CLIs)."""
    mock = MagicMock(return_value=_mock_run(stdout="2.1.98"))
    with patch("lu_machinery.utils.claude_version.subprocess.run", mock):
        supports_exclude_dynamic_system_prompt_sections(["claude"])
        assert mock.call_count == 1
        remember_support(["claude"], False)
        assert supports_exclude_dynamic_system_prompt_sections(["claude"]) is False
        assert mock.call_count == 1, "overwritten verdict must be served from cache"


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL, timeout=30)


def put(repo, path, text):
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.email', 'fixture@example.invalid')
    git(tmp_path, 'config', 'user.name', 'Fixture')
    files = {
        '.gitignore': 'docs/ignored.md\naudit/docs-inventory/\n',
        'docs/a.md': '---\nlifecycle: active\nsupersedes: b.md\n---\n'
                     '# A\n[B](b.md#section) [code](../scripts/example.py) '
                     '[excluded](session-state/private.md) [outside](https://example.invalid)\n'
                     '`scripts/example.py`\n',
        'docs/b.md': '# B\n',
        'docs/copy.md': '# B\n',
        'docs/spaced.md': '#  B\n',
        'docs/session-state/private.md': 'SENSITIVE SENTINEL',
        'docs/private/note.md': 'SENSITIVE SENTINEL',
        'wiki/page.md': 'SENSITIVE SENTINEL',
        'curriculum/a1/lesson.md': 'SENSITIVE SENTINEL',
        'data/book.md': 'SENSITIVE SENTINEL',
        '.claude/rules.md': 'SENSITIVE SENTINEL',
        'scripts/example.py': 'print("example")\n',
        'scripts/config/policy.yaml': 'value: true\n',
        **{p: '# Instruction\n[private](docs/b.md)\n' for p in
           ('AGENTS.md', 'CLAUDE.md', 'GEMINI.md', 'README.md')},
    }
    for path, text in files.items():
        put(tmp_path, path, text)
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'fixture')
    put(tmp_path, 'docs/ignored.md', 'IGNORED SENTINEL')
    put(tmp_path, 'docs/untracked.md', 'UNTRACKED SENTINEL')
    return tmp_path


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_tracked_only_exclusions_graph_and_duplicates(repo):
    manifest, graph = build(repo)
    rows = {r['path']: r for r in manifest['documents']}
    assert set(rows) == {'docs/a.md', 'docs/b.md', 'docs/copy.md', 'docs/spaced.md',
                         'AGENTS.md', 'CLAUDE.md', 'GEMINI.md', 'README.md'}
    output = canonical([manifest, graph]).decode()
    for forbidden in ('SENSITIVE', 'IGNORED', 'UNTRACKED', 'ignored.md', 'untracked.md',
                      'session-state/private.md', 'wiki/page.md', 'data/book.md'):
        assert forbidden not in output
    assert {'source': 'docs/a.md', 'target': 'docs/b.md', 'kind': 'supersedes'} in graph['edges']
    assert {'source': 'docs/a.md', 'target': 'scripts/example.py', 'kind': 'code_path'} in graph['edges']
    assert rows['docs/a.md']['unresolved_local_references'] == 1
    assert rows['docs/b.md']['inbound_references'] == 2
    assert rows['AGENTS.md']['outbound_references'] == 0
    assert ['docs/b.md', 'docs/copy.md'] in manifest['exact_duplicates']
    assert ['docs/b.md', 'docs/copy.md', 'docs/spaced.md'] in manifest['whitespace_duplicate_candidates']
    assert rows['docs/a.md']['candidate_lifecycle']['value'] == 'active'
    assert rows['docs/a.md']['history']['commit_count'] == 1


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_determinism_index_sparse_and_digest(repo, tmp_path_factory):
    first = build(repo)
    put(repo, 'docs/a.md', 'UNSTAGED SECRET')
    (repo / 'docs/b.md').unlink()  # A sparse/missing worktree file is still inventoried.
    assert canonical(build(repo)) == canonical(first)
    clone = tmp_path_factory.mktemp('clone')
    git(clone, 'clone', '-q', str(repo), '.')
    assert canonical(build(clone)) == canonical(first)
    git(repo, 'add', 'docs/a.md')
    changed, graph = build(repo)
    assert changed['git_commit'] == first[0]['git_commit']
    assert changed['inventory_digest'] != first[0]['inventory_digest']
    expected = changed.pop('inventory_digest')
    graph.pop('inventory_digest')
    assert digest(canonical({'manifest': changed, 'graph': graph})) == expected


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.parametrize(('body', 'error'), [
    ('---\nstatus: [broken\n---\n', 'frontmatter_invalid_yaml'),
    ('---\n- item\n---\n', 'frontmatter_not_mapping'),
    ('---\nstatus: true\n---\n', 'status_not_string'),
    ('---\nsupersedes: [one, 2]\n---\n', 'supersedes_not_string_or_list'),
    ('---\nstatus: active\nstatus: draft\n---\n', 'frontmatter_invalid_yaml'),
    ('---\na: &a [*a]\n---\n', 'frontmatter_alias_forbidden'),
    ('---\na: ' + '[' * 40 + 'x' + ']' * 40 + '\n---\n', 'frontmatter_complexity_limit'),
    ('---\nstatus: active', 'frontmatter_unterminated'),
    ('---\nstatus: mystery\n---\n', 'status_unrecognized'),
])
def test_malformed_metadata(body, error):
    _, errors = metadata(body)
    assert error in errors


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_symlinks_conflicts_and_analysis_limits(repo):
    (repo / 'docs/link.md').symlink_to('a.md')
    put(repo, 'docs/large.md', 'A' * (512 * 1024 + 1))
    (repo / 'docs/binary.md').write_bytes(b'\xff\xfe')
    put(repo, 'docs/marker.md', '**Status:** Binding\n**Authority**: contract\n')
    git(repo, 'add', 'docs/link.md', 'docs/large.md', 'docs/binary.md', 'docs/marker.md')
    manifest, _ = build(repo)
    rows = {r['path']: r for r in manifest['documents']}
    assert 'docs/link.md' not in rows
    assert rows['docs/large.md']['analysis'] == 'size_limit'
    assert rows['docs/binary.md']['analysis'] == 'non_utf8'
    assert rows['docs/marker.md']['authority_markers'] == ['authority', 'status']
    oid = git(repo, 'rev-parse', 'HEAD:docs/a.md').decode().strip()
    subprocess.run(['git', '-C', str(repo), 'update-index', '--index-info'],
                   input=f'100644 {oid} 1\tdocs/conflict.md\n'.encode(), check=True, timeout=30)
    with pytest.raises(ValueError, match='Unmerged'):
        build(repo)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_cli_identical_outputs_schema_and_output_guard(repo):
    script = Path(__import__("lu_machinery.docs.docs_inventory", fromlist=["__file__"]).__file__)
    command = [sys.executable, str(script), '--repo', str(repo)]
    subprocess.run(command, check=True, capture_output=True, timeout=60)
    output = repo / 'audit/docs-inventory'
    first = {p.name: p.read_bytes() for p in output.iterdir()}
    subprocess.run(command, check=True, capture_output=True, timeout=60)
    assert first == {p.name: p.read_bytes() for p in output.iterdir()}
    schema = json.loads('{\n  "type": "object",\n  "properties": {\n    "schema_version": {\n      "const": 1\n    },\n    "git_commit": {\n      "type": "string"\n    },\n    "source": {\n      "const": "git_index"\n    },\n    "tracked_file_digest": {\n      "type": "string",\n      "pattern": "^[0-9a-f]{64}$"\n    },\n    "inventory_digest": {\n      "type": "string",\n      "pattern": "^[0-9a-f]{64}$"\n    },\n    "history_scope": {\n      "type": "string"\n    },\n    "history_shallow": {\n      "type": "boolean"\n    },\n    "policy": {\n      "type": "object",\n      "properties": {\n        "parse_limit_bytes": {\n          "type": "integer",\n          "minimum": 0\n        },\n        "root_files": {\n          "type": "array",\n          "items": {\n            "type": "string"\n          }\n        },\n        "read_roots": {\n          "type": "array",\n          "items": {\n            "type": "string"\n          }\n        },\n        "excluded_components": {\n          "type": "array",\n          "items": {\n            "type": "string"\n          }\n        }\n      },\n      "required": [\n        "parse_limit_bytes",\n        "root_files",\n        "read_roots",\n        "excluded_components"\n      ],\n      "additionalProperties": false\n    },\n    "counts": {\n      "type": "object",\n      "properties": {\n        "documents": {\n          "type": "integer",\n          "minimum": 0\n        },\n        "eligible_tracked_files": {\n          "type": "integer",\n          "minimum": 0\n        },\n        "references": {\n          "type": "integer",\n          "minimum": 0\n        }\n      },\n      "required": [\n        "documents",\n        "eligible_tracked_files",\n        "references"\n      ],\n      "additionalProperties": false\n    },\n    "documents": {\n      "type": "array",\n      "items": {\n        "type": "object",\n        "properties": {\n          "path": {\n            "type": "string"\n          },\n          "type": {\n            "type": "string"\n          },\n          "bytes": {\n            "type": "integer",\n            "minimum": 0\n          },\n          "lines": {\n            "type": "integer",\n            "minimum": 0\n          },\n          "sha256": {\n            "type": "string",\n            "pattern": "^[0-9a-f]{64}$"\n          },\n          "git_blob": {\n            "type": "string"\n          },\n          "git_mode": {\n            "enum": [\n              "100644",\n              "100755"\n            ]\n          },\n          "history": {\n            "anyOf": [\n              {\n                "type": "object",\n                "properties": {\n                  "latest_commit": {\n                    "type": "string"\n                  },\n                  "latest_at": {\n                    "type": "string"\n                  },\n                  "first_at": {\n                    "type": "string"\n                  },\n                  "commit_count": {\n                    "type": "integer",\n                    "minimum": 0\n                  }\n                },\n                "required": [\n                  "latest_commit",\n                  "latest_at",\n                  "first_at",\n                  "commit_count"\n                ],\n                "additionalProperties": false\n              },\n              {\n                "type": "null"\n              }\n            ]\n          },\n          "location": {\n            "enum": [\n              "root_instruction",\n              "archive",\n              "generated",\n              "reference",\n              "binding_rule",\n              "adr",\n              "planning",\n              "operational",\n              "curated",\n              "supporting"\n            ]\n          },\n          "metadata_errors": {\n            "type": "array",\n            "items": {\n              "type": "string"\n            }\n          },\n          "authority_markers": {\n            "type": "array",\n            "items": {\n              "type": "string"\n            }\n          },\n          "candidate_lifecycle": {\n            "anyOf": [\n              {\n                "type": "null"\n              },\n              {\n                "type": "object",\n                "properties": {\n                  "value": {\n                    "enum": [\n                      "draft",\n                      "active",\n                      "superseded",\n                      "archive"\n                    ]\n                  },\n                  "confidence": {\n                    "enum": [\n                      "explicit",\n                      "location_only"\n                    ]\n                  },\n                  "evidence": {\n                    "type": "string"\n                  }\n                },\n                "required": [\n                  "value",\n                  "confidence",\n                  "evidence"\n                ],\n                "additionalProperties": false\n              }\n            ]\n          },\n          "unresolved_local_references": {\n            "type": "integer",\n            "minimum": 0\n          },\n          "analysis": {\n            "enum": [\n              "complete",\n              "non_utf8",\n              "size_limit"\n            ]\n          },\n          "inbound_references": {\n            "type": "integer",\n            "minimum": 0\n          },\n          "outbound_references": {\n            "type": "integer",\n            "minimum": 0\n          }\n        },\n        "required": [\n          "path",\n          "type",\n          "bytes",\n          "lines",\n          "sha256",\n          "git_blob",\n          "git_mode",\n          "history",\n          "location",\n          "metadata_errors",\n          "authority_markers",\n          "candidate_lifecycle",\n          "unresolved_local_references",\n          "analysis",\n          "inbound_references",\n          "outbound_references"\n        ],\n        "additionalProperties": false\n      }\n    },\n    "exact_duplicates": {\n      "type": "array",\n      "items": {\n        "type": "array",\n        "items": {\n          "type": "string"\n        }\n      }\n    },\n    "whitespace_duplicate_candidates": {\n      "type": "array",\n      "items": {\n        "type": "array",\n        "items": {\n          "type": "string"\n        }\n      }\n    }\n  },\n  "required": [\n    "schema_version",\n    "git_commit",\n    "source",\n    "tracked_file_digest",\n    "inventory_digest",\n    "history_scope",\n    "history_shallow",\n    "policy",\n    "counts",\n    "documents",\n    "exact_duplicates",\n    "whitespace_duplicate_candidates"\n  ],\n  "additionalProperties": false,\n  "$schema": "https://json-schema.org/draft/2020-12/schema",\n  "title": "Documentation inventory manifest v1"\n}\n')
    validate(json.loads(first['manifest.json']), schema)
    assert len(first['summary.md']) < 3000
    assert main(['--repo', str(repo), '--output', 'docs']) == 1
    (output / 'manifest.json').unlink()
    (output / 'manifest.json').symlink_to(repo / 'docs/a.md')
    assert main(['--repo', str(repo)]) == 1


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_reference_definitions_encoding_and_config(repo):
    put(repo, 'docs/space name.md', '# Space\n')
    put(repo, 'docs/links.md', '**Supersedes:** [previous](space%20name.md)\n'
                             '[ref][x]\n\n[x]: <space%20name.md#heading>\n'
                             '`scripts/config/policy.yaml`\n'
                             '```md\n[fake](b.md)\n```\n')
    git(repo, 'add', 'docs/space name.md', 'docs/links.md')
    _, graph = build(repo)
    edges = [e for e in graph['edges'] if e['source'] == 'docs/links.md']
    assert {'source': 'docs/links.md', 'target': 'docs/space name.md', 'kind': 'supersedes'} in edges
    assert {e['target'] for e in edges} == {'docs/space name.md', 'scripts/config/policy.yaml'}


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.parametrize('separator', ['\u0085', '\u2028', '\u2029'], ids=['NEL', 'LS', 'PS'])
def test_metadata_frontmatter_keeps_unicode_separators(separator):
    # #9556: frontmatter is framed on LF only, so YAML sees the original bytes.
    frontmatter = f'status: active\nsupersedes: "docs/a{separator}b.md"\n'
    facts, errors = metadata(f'---\r\n{frontmatter}---\r\nbody{separator}---\n')

    assert errors == []
    assert facts == {'status': 'active', 'supersedes': [yaml.safe_load(frontmatter)['supersedes']]}
