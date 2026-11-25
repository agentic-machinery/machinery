"""Daily timer renders, verifies and installs safely without live activation."""



import pytest
from lu_machinery.common.safe_unit_install import InstallError
from lu_machinery.orchestration import install_issue_quality_report_timer as installer
import json
import os
import re
import shutil
import subprocess
import yaml
from lu_machinery.common import repo_root
from lu_machinery.docs import catalogue as cat
from lu_machinery.docs import find as docs_find
from lu_machinery.docs.catalogue import main
from lu_machinery.docs.catalogue import repository_check
from lu_machinery.docs.catalogue import store_literal_errors
from lu_machinery.docs.catalogue import store_literals_in_source
from lu_machinery.docs.catalogue import validate
from tests.helpers.product_tree import configured_roots as configured_roots

def test_install_preview_apply_idempotence_and_symlink_refusal(tmp_path, monkeypatch):
    units = tmp_path / "units"
    rendered = installer.render_units(tmp_path / "repo")
    assert installer.check(rendered, units) == 1
    assert not units.exists()
    calls = []
    monkeypatch.setattr(installer, "systemctl_user", lambda *a: calls.append(a))
    assert installer.apply(rendered, units, enable=False) == 0
    assert calls == [("daemon-reload",)]
    assert installer.check(rendered, units) == 0
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in units.iterdir())
    assert installer.apply(rendered, units, enable=False) == 0
    target = units / installer.UNITS[0]
    target.unlink()
    target.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(InstallError):
        installer.apply(rendered, units, enable=False)


SCHEMA = json.loads('{\n  "$schema": "https://json-schema.org/draft/2020-12/schema",\n  "$id": "https://learn-ukrainian.github.io/schemas/docs-knowledge/catalogue.schema.json",\n  "title": "Document and data catalogue (#9412)",\n  "description": "Family-level catalogue of tracked docs/registry/evidence paths and logical data/ stores. Lifecycle values follow docs/architecture/docs-authority-lifecycle.md. Validated by scripts/docs/catalogue.py.",\n  "type": "object",\n  "additionalProperties": false,\n  "required": ["schema_version", "denominator", "status_mapping", "entries", "residual"],\n  "properties": {\n    "schema_version": {"const": 1},\n    "denominator": {\n      "type": "object",\n      "additionalProperties": false,\n      "required": ["tracked_roots", "local_store_root"],\n      "properties": {\n        "tracked_roots": {\n          "type": "array",\n          "items": {"enum": ["docs", "registry", "curriculum/l2-uk-en/evidence"]},\n          "minItems": 3,\n          "uniqueItems": true\n        },\n        "local_store_root": {"const": "data"}\n      }\n    },\n    "status_mapping": {\n      "description": "Issue #9412 status words mapped onto the existing lifecycle vocabulary.",\n      "type": "object",\n      "additionalProperties": false,\n      "required": ["current", "historical", "superseded"],\n      "properties": {\n        "current": {"const": "active"},\n        "historical": {"const": "archive"},\n        "superseded": {"const": "superseded"}\n      }\n    },\n    "entries": {\n      "type": "array",\n      "minItems": 1,\n      "items": {"$ref": "#/$defs/entry"}\n    },\n    "store_literal_exemptions": {\n      "description": "data/<name>.db|.sqlite|.sqlite3 names that scripts/ code mentions but that are not stores of this repository; every other such name must be claimed by a data_store entry.",\n      "type": "array",\n      "items": {\n        "type": "object",\n        "additionalProperties": false,\n        "required": ["literal", "reason"],\n        "properties": {\n          "literal": {"$ref": "#/$defs/storeName"},\n          "reason": {"$ref": "#/$defs/text"}\n        }\n      }\n    },\n    "store_registry": {"$ref": "#/$defs/storeRegistry"},\n    "residual": {\n      "description": "Tracked paths whose lifecycle cannot be classified yet. Each path still belongs to a family; the family default does not apply to it.",\n      "type": "array",\n      "items": {\n        "type": "object",\n        "additionalProperties": false,\n        "required": ["path", "owner", "reason"],\n        "properties": {\n          "path": {"$ref": "#/$defs/repoPath"},\n          "owner": {"$ref": "#/$defs/owner"},\n          "reason": {"$ref": "#/$defs/text"}\n        }\n      }\n    }\n  },\n  "$defs": {\n    "text": {\n      "description": "Free text with at least one non-space character and no C0, DEL or C1 control. Whitespace is the explicit set U+0020, U+00A0, U+1680, U+2000-U+200A, U+2028, U+2029, U+202F, U+205F, U+3000 and U+FEFF plus every control. Every pattern in this schema uses explicit escaped classes only (never the shorthand classes s, S, w, W, d, D, b or the dot) and ends with (?![^\\\\n]|\\\\n), never $ (which in Python also matches before a final newline), so Python re and ECMA-262 engines give the same verdict for every string, including U+0085, U+2028, U+2029 and U+FEFF.",\n      "type": "string",\n      "minLength": 3,\n      "pattern": "[^ \\\\u00a0\\\\u1680\\\\u2000-\\\\u200a\\\\u2028\\\\u2029\\\\u202f\\\\u205f\\\\u3000\\\\ufeff\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]",\n      "not": {"pattern": "[\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]"}\n    },\n    "repoPath": {\n      "description": "Repository-relative POSIX path; never absolute, never parent-relative.",\n      "type": "string",\n      "minLength": 1,\n      "pattern": "^(?!/)(?![^\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]*(?:^|/)\\\\.\\\\.(?:/|#|(?![^\\\\n]|\\\\n)))[^\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]+(?![^\\\\n]|\\\\n)"\n    },\n    "glob": {\n      "description": "Catalogue glob dialect (also used by scripts/docs/catalogue.py): literal characters; \'*\' matches within one path segment, never \'/\'; \'?\' matches one character within a segment; \'**\' is a whole segment spanning one or more segments (zero or more when a segment follows). Character classes (\'[...]\'), braces (\'{a,b}\'), empty, \'.\' and \'..\' segments, a leading or trailing \'/\' and control characters are rejected: list each path instead. Catch-all rule (validator): the first segment below the tracked root must be literal, so \'docs/**\', \'docs/*/**\', \'docs/a*/**\' and \'**\' are errors.",\n      "type": "string",\n      "minLength": 1,\n      "pattern": "^(?!/)(?![^\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]*//)(?![^\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]*/(?![^\\\\n]|\\\\n))(?!(?:[^\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]*/)?\\\\.\\\\.?(?:/|(?![^\\\\n]|\\\\n)))(?![^\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]*[^/]\\\\*\\\\*)(?![^\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]*\\\\*\\\\*[^/])[^\\\\[\\\\]{}\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]+(?![^\\\\n]|\\\\n)"\n    },\n    "storeName": {\n      "description": "A logical store under data/ in the catalogue glob dialect (see $defs/glob); a trailing \'/\' claims the subtree.",\n      "type": "string",\n      "pattern": "^(?![^\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]*//)data/(?!(?:[^\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]*/)?\\\\.\\\\.?(?:/|(?![^\\\\n]|\\\\n)))(?![^\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]*[^/]\\\\*\\\\*)(?![^\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]*\\\\*\\\\*[^/])[^\\\\[\\\\]{}\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]+(?![^\\\\n]|\\\\n)"\n    },\n    "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*[a-z0-9](?![^\\\\n]|\\\\n)"},\n    "owner": {\n      "description": "A stream key from scripts/config/issue_streams.yaml or an area key from scripts/config/area_assignments.yaml (checked by the validator).",\n      "type": "string",\n      "pattern": "^[a-z0-9][a-z0-9-]*(?![^\\\\n]|\\\\n)"\n    },\n    "lifecycle": {"enum": ["draft", "active", "superseded", "archive"]},\n    "supersessionTarget": {\n      "description": "A tracked repository path (optionally with #section) or id:<catalogue entry id>.",\n      "type": "string",\n      "minLength": 3,\n      "pattern": "^(?:id:[a-z0-9][a-z0-9-]*[a-z0-9]|(?!/)(?![^\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]*(?:^|/)\\\\.\\\\.(?:/|#|(?![^\\\\n]|\\\\n)))[^\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]+)(?![^\\\\n]|\\\\n)"\n    },\n    "query": {\n      "type": "array",\n      "minItems": 1,\n      "items": {\n        "type": "object",\n        "additionalProperties": false,\n        "required": ["surface", "how"],\n        "properties": {\n          "surface": {"enum": ["cli", "api", "mcp", "sqlite", "git_grep"]},\n          "how": {"$ref": "#/$defs/text"}\n        }\n      }\n    },\n    "entrypoints": {\n      "description": "What is current for a topic: topic -> exact tracked path or path#section.",\n      "type": "array",\n      "items": {\n        "type": "object",\n        "additionalProperties": false,\n        "required": ["topic", "path"],\n        "properties": {\n          "topic": {"$ref": "#/$defs/text"},\n          "path": {"$ref": "#/$defs/repoPath"}\n        }\n      }\n    },\n    "override": {\n      "type": "object",\n      "additionalProperties": false,\n      "required": ["path", "lifecycle", "evidence"],\n      "properties": {\n        "path": {"$ref": "#/$defs/repoPath"},\n        "lifecycle": {"$ref": "#/$defs/lifecycle"},\n        "superseded_by": {"$ref": "#/$defs/supersessionTarget"},\n        "evidence": {"$ref": "#/$defs/text"}\n      },\n      "if": {"properties": {"lifecycle": {"const": "superseded"}}},\n      "then": {"required": ["superseded_by"]},\n      "else": {"not": {"required": ["superseded_by"]}}\n    },\n    "common": {\n      "type": "object",\n      "required": ["id", "kind", "purpose", "keywords", "lifecycle", "owner", "query", "content_searchable"],\n      "properties": {\n        "id": {"$ref": "#/$defs/id"},\n        "purpose": {"$ref": "#/$defs/text"},\n        "keywords": {\n          "type": "array",\n          "minItems": 1,\n          "uniqueItems": true,\n          "items": {"type": "string", "minLength": 2, "pattern": "[^ \\\\u00a0\\\\u1680\\\\u2000-\\\\u200a\\\\u2028\\\\u2029\\\\u202f\\\\u205f\\\\u3000\\\\ufeff\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]", "not": {"pattern": "[\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]"}}\n        },\n        "lifecycle": {"$ref": "#/$defs/lifecycle"},\n        "superseded_by": {"$ref": "#/$defs/supersessionTarget"},\n        "owner": {"$ref": "#/$defs/owner"},\n        "entrypoints": {"$ref": "#/$defs/entrypoints"},\n        "query": {"$ref": "#/$defs/query"},\n        "content_searchable": {"type": "boolean"},\n        "notes": {"$ref": "#/$defs/text"}\n      },\n      "if": {"properties": {"lifecycle": {"const": "superseded"}}, "required": ["lifecycle"]},\n      "then": {"required": ["superseded_by"]},\n      "else": {"not": {"required": ["superseded_by"]}}\n    },\n    "entry": {\n      "description": "The kind selects the shape: data_store entries name logical stores, every other kind names tracked globs.",\n      "if": {"type": "object", "properties": {"kind": {"const": "data_store"}}, "required": ["kind"]},\n      "then": {"$ref": "#/$defs/storeEntry"},\n      "else": {"$ref": "#/$defs/familyEntry"}\n    },\n    "familyEntry": {\n      "description": "Tracked family: globs under the tracked roots.",\n      "type": "object",\n      "unevaluatedProperties": false,\n      "allOf": [{"$ref": "#/$defs/common"}],\n      "required": ["paths"],\n      "properties": {\n        "kind": {"enum": ["doc_family", "registry", "evidence", "resource_catalogue", "generated"]},\n        "paths": {\n          "type": "array",\n          "minItems": 1,\n          "uniqueItems": true,\n          "items": {"$ref": "#/$defs/glob"}\n        },\n        "overrides": {"type": "array", "items": {"$ref": "#/$defs/override"}}\n      }\n    },\n    "storeEntry": {\n      "description": "Logical local store under data/: identity and producers only, never a host path.",\n      "type": "object",\n      "unevaluatedProperties": false,\n      "allOf": [{"$ref": "#/$defs/common"}],\n      "required": ["store", "producer", "local_only"],\n      "properties": {\n        "kind": {"const": "data_store"},\n        "store": {\n          "type": "array",\n          "minItems": 1,\n          "uniqueItems": true,\n          "items": {"$ref": "#/$defs/storeName"}\n        },\n        "producer": {\n          "type": "array",\n          "uniqueItems": true,\n          "items": {"$ref": "#/$defs/repoPath"}\n        },\n        "producer_note": {"$ref": "#/$defs/text"},\n        "local_only": {"type": "boolean"}\n      }\n    },\n    "writerRef": {\n      "description": "A write entry point: a tracked file, \':\' and a module-level function or Class.method defined in it (checked by scripts/docs/store_census.py).",\n      "type": "string",\n      "pattern": "^(?!/)[^:\\\\u0000-\\\\u001f\\\\u007f-\\\\u009f]+:[A-Za-z_][A-Za-z0-9_]*(?:[.][A-Za-z_][A-Za-z0-9_]*)*(?![^\\\\n]|\\\\n)"\n    },\n    "storeFacts": {\n      "description": "Facts every store and family row records (#11092). protection keep|rebuild|disposable is never a deletion authorization; backup is the fact scripts/config/backup-allowlist.json gives today.",\n      "type": "object",\n      "required": ["owner", "kind", "writers", "journal", "busy_ms", "busy_source", "protection", "backup", "retention"],\n      "properties": {\n        "owner": {"$ref": "#/$defs/owner"},\n        "kind": {"enum": ["operational", "cache", "reference", "derived", "job_state", "telemetry", "staged", "orphan"]},\n        "writers": {"type": "array", "uniqueItems": true, "items": {"$ref": "#/$defs/writerRef"}},\n        "writer_note": {"$ref": "#/$defs/text"},\n        "journal": {"enum": ["wal", "delete", "unknown"]},\n        "busy_ms": {"type": ["integer", "null"], "minimum": 0},\n        "busy_source": {"enum": ["default", "explicit", "no_writer", "not_applicable"]},\n        "busy_setters": {\n          "description": "For busy_source explicit when the writers open the store through a shared helper: the functions that set the busy timeout (checked against busy_ms instead of the writers).",\n          "type": "array", "minItems": 1, "uniqueItems": true, "items": {"$ref": "#/$defs/writerRef"}\n        },\n        "protection": {"enum": ["keep", "rebuild", "disposable"]},\n        "backup": {"enum": ["keep", "drop", "none"]},\n        "protection_gap": {"$ref": "#/$defs/text"},\n        "retention": {"$ref": "#/$defs/text"},\n        "note": {"$ref": "#/$defs/text"}\n      }\n    },\n    "storeRow": {\n      "description": "One named physical SQLite store, present or declared in code but absent. <git-common-dir>/ prefixes a path in the Git common directory.",\n      "type": "object",\n      "unevaluatedProperties": false,\n      "allOf": [{"$ref": "#/$defs/storeFacts"}],\n      "required": ["path", "activity", "presence"],\n      "properties": {\n        "path": {"$ref": "#/$defs/repoPath"},\n        "activity": {"enum": ["active", "dormant", "staged", "orphan", "not_created"]},\n        "presence": {"enum": ["present", "declared_absent"]},\n        "absent_reason": {"$ref": "#/$defs/text"},\n        "family": {"$ref": "#/$defs/id"},\n        "catalogue_entry": {"$ref": "#/$defs/id"}\n      }\n    },\n    "familyRow": {\n      "description": "Stores at caller-chosen or per-key paths: the code\'s path rule and, when knowable, a census glob with an example.",\n      "type": "object",\n      "unevaluatedProperties": false,\n      "allOf": [{"$ref": "#/$defs/storeFacts"}],\n      "required": ["id", "path_rule", "glob"],\n      "properties": {\n        "id": {"$ref": "#/$defs/id"},\n        "path_rule": {"$ref": "#/$defs/text"},\n        "glob": {"anyOf": [{"$ref": "#/$defs/glob"}, {"type": "null"}]},\n        "example": {"$ref": "#/$defs/repoPath"},\n        "literals": {"type": "array", "uniqueItems": true, "items": {"$ref": "#/$defs/storeName"}}\n      }\n    },\n    "evidenceDir": {\n      "description": "A top-level batch_state/ directory the census does not walk; its files are classified by this row.",\n      "type": "object",\n      "additionalProperties": false,\n      "required": ["path", "owner", "example", "backup", "retention"],\n      "properties": {\n        "path": {"$ref": "#/$defs/glob"},\n        "owner": {"$ref": "#/$defs/owner"},\n        "example": {"$ref": "#/$defs/repoPath"},\n        "backup": {"enum": ["keep", "drop", "none"]},\n        "retention": {"$ref": "#/$defs/text"}\n      }\n    },\n    "nonStore": {\n      "description": "SQLite-shaped files that are not project stores, by exact paths or by a glob with an example.",\n      "type": "object",\n      "additionalProperties": false,\n      "required": ["id", "class", "owner", "backup", "retention"],\n      "properties": {\n        "id": {"$ref": "#/$defs/id"},\n        "class": {"enum": ["zero_byte_placeholder", "coverage_data", "backup_copy", "test_copy", "experiment_copy", "probe_evidence"]},\n        "paths": {"type": "array", "minItems": 1, "uniqueItems": true, "items": {"$ref": "#/$defs/repoPath"}},\n        "glob": {"$ref": "#/$defs/glob"},\n        "example": {"$ref": "#/$defs/repoPath"},\n        "owner": {"$ref": "#/$defs/owner"},\n        "backup": {"enum": ["keep", "drop", "none"]},\n        "retention": {"$ref": "#/$defs/text"}\n      }\n    },\n    "storeRegistry": {\n      "description": "Every SQLite store the project owns (#11092); validated and censused by scripts/docs/store_census.py.",\n      "type": "object",\n      "additionalProperties": false,\n      "required": ["census_roots", "stores", "families", "evidence_dirs", "non_stores"],\n      "properties": {\n        "census_roots": {"const": ["data", ".agent", ".mcp", "batch_state"]},\n        "stores": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/storeRow"}},\n        "families": {"type": "array", "items": {"$ref": "#/$defs/familyRow"}},\n        "evidence_dirs": {"type": "array", "items": {"$ref": "#/$defs/evidenceDir"}},\n        "non_stores": {"type": "array", "items": {"$ref": "#/$defs/nonStore"}}\n      }\n    }\n  }\n}\n')


BOM = chr(0xFEFF)


NEL = chr(0x85)


LS, PS = chr(0x2028), chr(0x2029)


def entry(eid, paths, **extra):
    data = {'id': eid, 'kind': 'doc_family', 'paths': paths, 'purpose': f'{eid} documents',
            'keywords': [eid], 'lifecycle': 'active', 'owner': 'infra-harness',
            'query': [{'surface': 'git_grep', 'how': f'git grep -n -F x -- {paths[0]}'}],
            'content_searchable': True}
    data.update(extra)
    return data


def store(eid, names, **extra):
    data = {'id': eid, 'kind': 'data_store', 'store': names, 'producer': ['scripts/build.py'], 'local_only': True,
            'purpose': f'{eid} store', 'keywords': [eid], 'lifecycle': 'active', 'owner': 'infra-harness',
            'query': [{'surface': 'sqlite', 'how': 'read-only SQL'}], 'content_searchable': False}
    data.update(extra)
    return data


def catalogue(*entries, **extra):
    return {'schema_version': 1,
            'denominator': {'tracked_roots': ['docs', 'registry', 'curriculum/l2-uk-en/evidence'],
                            'local_store_root': 'data'},
            'status_mapping': {'current': 'active', 'historical': 'archive', 'superseded': 'superseded'},
            'entries': list(entries), 'residual': [], **extra}


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=True, timeout=30).stdout


def make_repo(root, data, files):
    root.mkdir(parents=True, exist_ok=True)
    git(root, 'init', '-q')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    git(root, 'config', 'user.name', 'Fixture')
    base = {
        'docs/knowledge/catalogue.schema.json': json.dumps(SCHEMA),
        'docs/knowledge/catalogue.yaml': yaml.safe_dump(data, allow_unicode=True),
        'scripts/build.py': "print('x')\n",
        'scripts/config/issue_streams.yaml': 'streams:\n  docs-knowledge: {epics: [1]}\n',
        'scripts/config/area_assignments.yaml': 'assignments:\n  infra-harness: {slots: []}\n',
    }
    for rel, text in {**base, **files}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding='utf-8')
    git(root, 'add', '.')
    return root


def recatalogue(repo, data):
    (repo / 'docs/knowledge/catalogue.yaml').write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')
    git(repo, 'add', '.')


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize('source, expected', [
    ("DB = 'data/main.db'\n", {'data/main.db'}),
    ("p = f'{root}/data/telemetry/x.sqlite3'\n", {'data/telemetry/x.sqlite3'}),
    ("p = ROOT / 'data' / 'side' / 'k.sqlite'\n", {'data/side/k.sqlite'}),
    ("DATA_DIR = ROOT / 'data'\nDB = DATA_DIR / 'v.db'\n", {'data/v.db'}),
    ("BASE = ROOT / 'data' / 'lexicon'\nDB = BASE / 'cache' / 'raw.sqlite'\n", {'data/lexicon/cache/raw.sqlite'}),
    ("help = 'Defaults to data/a.db.'\n", {'data/a.db'}),
    ("# reads data/commented.db\n", {'data/commented.db'}),  # comments count: stricter, never laxer
    ("x = 'metadata/m.db'; y = 'test_data/t.db'; z = 'data/x.json'\n", set()),
    ("p = ROOT / 'data' / name / 'x.db'\n", set()),  # a dynamic segment is not a literal store name
    ("p = ROOT / 'other' / 'x.db'\n", set()),
    ("def broken(:\n  'data/syntax.db'\n", {'data/syntax.db'}),  # text match survives a parse error
])
def test_store_names_in_source(source, expected):
    assert store_literals_in_source(source) == expected


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize('text, expected', [
    # the reviewer's mutations
    ('sqlite3 data/unclaimed9412.db "select 1"\n', {'data/unclaimed9412.db'}),  # .sh, unquoted
    ("const db = new Database('data/unclaimed9412.db');\n", {'data/unclaimed9412.db'}),  # .js
    ("DB = 'data/словник9412.db'\n", {'data/словник9412.db'}),
    ("DB = 'data/new store9412.db'\n", {'data/new store9412.db'}),
    # shell words: unquoted, single-quoted, double-quoted, escaped space, assignment, redirect
    ("cp 'data/single9412.db' /tmp\n", {'data/single9412.db'}),
    ('DB="data/double9412.sqlite3"; sqlite3 "$DB"\n', {'data/double9412.sqlite3'}),
    ('cp data/new\\ store9412.db x\n', {'data/new store9412.db'}),
    ('sqlite3 data/a9412.db<q.sql>out.txt\n', {'data/a9412.db'}),
    ('"data/двоє слів/каталог.duckdb"\n', {'data/двоє слів/каталог.duckdb'}),
    # config and markup
    ('store: data/yaml9412.db\nother: [data/b9412.sqlite, data/c9412.db]\n',
     {'data/yaml9412.db', 'data/b9412.sqlite', 'data/c9412.db'}),
    ('{"db": "data/json9412.db"}\n', {'data/json9412.db'}),
    ('ExecStart=/usr/bin/tool --db=../data/unit9412.db\n', {'data/unit9412.db'}),
    # comments count (one exemption at worst; a missed store would pass the gate)
    ('# reads data/comment9412.db at start\n', {'data/comment9412.db'}),
    ('// data/jscomment9412.db\n', {'data/jscomment9412.db'}),
    # a sidecar or copy names its store
    ('data/main.db-wal data/main.db.bak\n', {'data/main.db'}),
])
def test_store_names_in_any_text(text, expected):
    assert cat.store_names_in_text(text) == expected
    assert store_literals_in_source(text, python=False) == expected


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize('text', [
    'metadata/m.db test_data/t.db my-data/x.db .data/y.db\n',  # not the data/ root
    'data/x.dbx data/x.json data/x.db_old data/xdb\n',  # not a store extension
    "f'data/{name}.db' data/$NAME.db data/*.db data/x?.db data/[ab].db\n",  # built at run time or a glob
    "'data/../escape.db' 'data/./here.db' data//double.db\n",  # dot and empty segments
    '# see data/ and the backups/x.db\n',  # prose: a space after data/ ends the word
    "msg = 'see data/ and backups/x.db'\n",  # a quoted segment never starts with a space
    "msg = 'data/dir /x.db'\n",  # nor ends with one
])
def test_text_that_names_no_store(text):
    assert cat.store_names_in_text(text) == set()


@pytest.mark.usefixtures('configured_roots')
def test_store_literal_errors_name_the_file_and_the_fix():
    data = catalogue(store('main', ['data/main.db']), store('tele', ['data/telemetry/']))
    literals = {'data/main.db': ['scripts/a.py'], 'data/telemetry/x.db': ['scripts/b.py'],
                'data/new.db': ['scripts/c.py', 'scripts/d.py']}
    errors = store_literal_errors(data, literals)
    assert len(errors) == 1
    assert "'data/new.db' (named in scripts/c.py, scripts/d.py) has no data_store entry" in errors[0]


@pytest.mark.usefixtures('configured_roots')
def test_exemptions_must_be_live_and_not_redundant():
    data = catalogue(store('main', ['data/main.db']), store_literal_exemptions=[
        {'literal': 'data/main.db', 'reason': 'redundant'}, {'literal': 'data/gone.db', 'reason': 'stale entry'},
        {'literal': 'data/example.db', 'reason': 'help text only'}])
    errors = store_literal_errors(data, {'data/main.db': ['scripts/a.py'], 'data/example.db': ['scripts/b.py']})
    assert any("'data/main.db' is claimed by a data_store entry and also exempted" in e for e in errors)
    assert any("'data/gone.db' is no longer named in lu_machinery/ and scripts/" in e for e in errors)
    assert not any('example.db' in e for e in errors)
    assert validate(data, SCHEMA, [], {'infra-harness'}).schema_ok


@pytest.mark.usefixtures('configured_roots')
def test_check_fails_on_an_unclaimed_store_and_suggests_a_stub(tmp_path, capsys):
    repo = make_repo(tmp_path / 'r', catalogue(entry('knowledge', ['docs/knowledge/**'], owner='docs-knowledge')),
                     {'scripts/tool.py': "DB = 'data/fresh.db'\n"})
    assert main(['check', '--repo', str(repo), '--suggest']) == 1
    out = capsys.readouterr().out
    assert "ERROR store literal 'data/fresh.db' (named in scripts/tool.py)" in out
    stub_text = out[out.index('  # data/ store named in lu_machinery/ and scripts/'):]
    stub = yaml.safe_load('\n'.join(stub_text.splitlines()[1:]))[0]
    assert stub['store'] == ['data/fresh.db']
    data = yaml.safe_load((repo / 'docs/knowledge/catalogue.yaml').read_text())
    data['entries'].append(store('data-fresh', ['data/fresh.db']))
    recatalogue(repo, data)
    assert main(['check', '--repo', str(repo)]) == 0


def test_owner_keys_come_from_package_without_consumer_registry_copies(tmp_path, monkeypatch):
    repo = make_repo(
        tmp_path / 'consumer',
        catalogue(
            entry('knowledge', ['docs/knowledge/**'], owner='fixture-stream'),
            entry('guide', ['docs/guide/**'], owner='fixture-area'),
        ),
        {'docs/guide/index.md': '# Sailing handbook\n'},
    )
    git(repo, 'rm', '-f', '--', 'scripts/config/issue_streams.yaml', 'scripts/config/area_assignments.yaml')

    package = tmp_path / 'package'
    config = package / 'config'
    config.mkdir(parents=True)
    (config / 'issue_streams.yaml').write_text('streams: {fixture-stream: {}}\n', encoding='utf-8')
    (config / 'area_assignments.yaml').write_text('assignments: {fixture-area: {}}\n', encoding='utf-8')
    monkeypatch.setattr(repo_root, '_MACHINERY_ROOT', package)
    profile_path = tmp_path / 'profile.v1.json'
    profile_path.write_text(json.dumps({
        'schema': 'lu-machinery.profile', 'version': 1, 'profile_id': 'fixture',
        'github_slug': 'fixture/consumer', 'config': {},
    }), encoding='utf-8')
    monkeypatch.setenv('LU_PROFILE_PATH', str(profile_path))
    monkeypatch.setenv('LU_GITHUB_SLUG', 'fixture/consumer')
    monkeypatch.delenv('LEARN_UKRAINIAN_GITHUB_SLUG', raising=False)

    assert main(['check', '--repo', str(repo)]) == 0
    result = docs_find.find('sailing handbook', family='guide', repo=repo)
    assert not result['coverage']['incomplete']
    assert result['hits'][0]['path'] == 'docs/guide/index.md'


@pytest.mark.usefixtures('configured_roots')
def test_store_scan_reads_tracked_regular_code_only(tmp_path):
    repo = make_repo(tmp_path / 'r', catalogue(entry('knowledge', ['docs/knowledge/**'], owner='docs-knowledge')),
                     {'scripts/tool.py': "DB = 'data/a.db'\n"})
    (repo / 'scripts/untracked.py').write_text("DB = 'data/untracked.db'\n")
    os.symlink(repo / 'scripts/tool.py', repo / 'scripts/link.py')
    git(repo, 'add', 'scripts/link.py')
    scan = cat.store_literals(repo)
    assert scan.stores == {'data/a.db': ['scripts/tool.py']}
    assert scan.unread == ['scripts/link.py'] and scan.withheld == []


SCRIPT_SURFACE = {
    'scripts/run.sh': 'sqlite3 data/unquoted9412.db ".tables"\ncp \'data/single9412.db\' x\nDB="data/double9412.db"\n',
    'scripts/web/app.js': "const db = new Database('data/js9412.db');\n",
    'scripts/web/app.mjs': "export const DB = 'data/mjs9412.sqlite';\n",
    'scripts/web/app.ts': 'const DB: string = "data/ts9412.duckdb";\n',
    'scripts/config/stores.yaml': 'store: data/yaml9412.db\n',
    'scripts/config/stores.toml': 'db = "data/toml9412.db"\n',
    'scripts/config/stores.json': '{"db": "data/json9412.db"}\n',
    'scripts/README.md': 'Run against `data/markdown9412.db`.\n',
    'scripts/templates/x.j2': '{{ root }}/data/jinja9412.db\n',
    'scripts/units/x.service': 'ExecStart=/bin/tool --db data/unit9412.db\n',
    'scripts/units/x.timer': '# keeps data/timer9412.db fresh\n',
    'scripts/bin/tool': '#!/usr/bin/env bash\nexec sqlite3 data/shebang9412.db\n',
    'scripts/notes': 'no shebang, still text: data/plain9412.db\n',
    'scripts/unicode.py': "DB = 'data/словник9412.db'\nOTHER = 'data/new store9412.db'\n",
    'scripts/x.ps1': '$db = "data/ps9412.db"\n',
}


@pytest.mark.usefixtures('configured_roots')
def test_store_scan_covers_every_tracked_text_file_under_scripts(tmp_path, capsys):
    """The surface is the index under scripts/, not a list of extensions."""
    repo = make_repo(tmp_path / 'r', catalogue(entry('knowledge', ['docs/knowledge/**'], owner='docs-knowledge')),
                     SCRIPT_SURFACE)
    scan = cat.store_literals(repo)
    expected = {name for text in SCRIPT_SURFACE.values() for name in cat.store_names_in_text(text)}
    assert set(scan.stores) == expected and len(expected) == 18
    assert scan.stores['data/new store9412.db'] == ['scripts/unicode.py']
    assert scan.scanned == len(SCRIPT_SURFACE) + 3 and scan.unread == [] and scan.withheld == []
    assert main(['check', '--repo', str(repo)]) == 1
    out = capsys.readouterr().out
    for name in expected:
        assert f'ERROR store literal {name!r}' in out


@pytest.mark.usefixtures('configured_roots')
def test_paths_outside_scripts_and_lookalike_names_are_not_required(tmp_path, capsys):
    repo = make_repo(tmp_path / 'r', catalogue(entry('knowledge', ['docs/knowledge/**'], owner='docs-knowledge')),
                     {'tests/test_fixture.py': "DB = 'data/fixture9412.db'\n",
                      'tests/fixtures/run.sh': 'sqlite3 data/fixture9412.db\n',
                      'docs/knowledge/note.md': 'data/doc9412.db\n',
                      'scripts/lookalike.py': "M = 'metadata/m9412.db'; T = 'test_data/t9412.db'\n"
                                              "J = 'data/j9412.json'; F = f'data/{name}.db'\n"})
    assert cat.store_literals(repo).stores == {}
    assert main(['check', '--repo', str(repo)]) == 0, capsys.readouterr().out


@pytest.mark.usefixtures('configured_roots')
def test_store_scan_reads_the_index_blob_not_the_worktree(tmp_path):
    repo = make_repo(tmp_path / 'r', catalogue(entry('knowledge', ['docs/knowledge/**'], owner='docs-knowledge')),
                     {'scripts/tool.sh': 'sqlite3 data/staged9412.db\n'})
    (repo / 'scripts/tool.sh').write_text('sqlite3 data/unstaged9412.db\n')  # edited, not staged
    assert list(cat.store_literals(repo).stores) == ['data/staged9412.db']
    (repo / 'scripts/tool.sh').unlink()  # a sparse or deleted worktree file is still read from the index
    assert list(cat.store_literals(repo).stores) == ['data/staged9412.db']


@pytest.mark.usefixtures('configured_roots')
def test_store_scan_skips_binary_and_fails_closed_on_unreadable_or_withheld(tmp_path, capsys):
    repo = make_repo(tmp_path / 'r', catalogue(entry('knowledge', ['docs/knowledge/**'], owner='docs-knowledge')),
                     {'scripts/cache/helper.py': "DB = 'data/hidden9412.db'\n"})
    (repo / 'scripts/blob.bin').write_bytes(b'\0\1data/binary9412.db')
    (repo / 'scripts/latin1.txt').write_bytes('café data/latin9412.db\n'.encode('latin-1'))
    git(repo, 'add', '.')
    scan = cat.store_literals(repo)
    assert scan.stores == {} and scan.binary == 1
    assert scan.unread == ['scripts/latin1.txt'] and scan.withheld == ['scripts/cache/helper.py']
    assert main(['check', '--repo', str(repo)]) == 1
    out = capsys.readouterr().out
    assert "cannot read tracked file scripts/latin1.txt (not a regular UTF-8 blob)" in out
    assert 'tracked file scripts/cache/helper.py is withheld by the privacy gate' in out
    assert 'hidden9412' not in out and 'latin9412' not in out  # bodies are never echoed


def marker_repo(tmp_path, files, *overrides):
    data = catalogue(entry('knowledge', ['docs/knowledge/**'], owner='docs-knowledge'),
                     entry('guide', ['docs/guide/**'], overrides=list(overrides)),
                     entry('guide-private', ['docs/guide/private/**'], content_searchable=False))
    return make_repo(tmp_path / 'r', data, {'docs/guide/private/p.md': '# Private\n', **files})


SUPERSEDED = {'path': 'docs/guide/old.md', 'lifecycle': 'superseded', 'superseded_by': 'docs/guide/new.md',
              'evidence': 'Replaced by the new guide.'}


@pytest.mark.usefixtures('configured_roots')
def test_markers_are_required_for_superseded_markdown_overrides_and_mark_writes_them(tmp_path, capsys):
    repo = marker_repo(tmp_path, {'docs/guide/old.md': '# Old\n', 'docs/guide/new.md': '# New\n'}, SUPERSEDED)
    report, _ = repository_check(repo)
    errors = [e for e in report.errors if e.startswith('docs/guide/old.md')]
    assert len(errors) == 2 and 'lifecycle: superseded' in errors[0] and 'banner' in errors[1]
    assert main(['mark', '--repo', str(repo)]) == 1  # lists, writes nothing
    assert (repo / 'docs/guide/old.md').read_text() == '# Old\n'
    assert main(['mark', '--repo', str(repo), '--apply']) == 0
    capsys.readouterr()
    git(repo, 'add', '.')
    assert repository_check(repo)[0].errors == []
    assert main(['mark', '--repo', str(repo)]) == 0


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize('front, message', [
    ('lifecycle: archive', "front-matter lifecycle 'archive' disagrees with the catalogue ('active'"),
    ('superseded_by: docs/guide/new.md', 'front-matter superseded_by'),
    ('lifecycle: obsolete', 'front matter lifecycle unrecognized'),
])
def test_front_matter_that_disagrees_with_the_catalogue_fails(tmp_path, front, message):
    repo = marker_repo(tmp_path, {'docs/guide/a.md': f'---\n{front}\n---\n# A\n', 'docs/guide/new.md': '# N\n'})
    assert any(message in e for e in repository_check(repo)[0].errors), repository_check(repo)[0].errors


@pytest.mark.usefixtures('configured_roots')
def test_front_matter_that_agrees_passes_and_private_files_are_never_read(tmp_path):
    repo = marker_repo(tmp_path, {'docs/guide/a.md': '---\nlifecycle: active\n---\n# A\n',
                                  'docs/guide/private/p.md': '---\nlifecycle: archive\n---\n'})
    report, _ = repository_check(repo)
    assert report.errors == [] and report.markers_unverifiable == 0


def schema_patterns(node=SCHEMA, where='#'):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == 'pattern':
                yield where, value
            else:
                yield from schema_patterns(value, f'{where}/{key}')
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from schema_patterns(value, f'{where}/{index}')


@pytest.mark.usefixtures('configured_roots')
def test_patterns_use_no_engine_dependent_constructs():
    found = list(schema_patterns())
    assert len(found) >= 10
    for where, pattern in found:
        # Remove escaped characters and bracket classes, then nothing engine-dependent may remain.
        bare = re.sub(r'\[(?:\\.|[^\]\\])*\]', '', re.sub(r'\\u[0-9a-f]{4}', '', pattern))
        assert not re.search(r'\\[sSwWdDbBAZz]', pattern), where
        assert '$' not in pattern, where
        assert '.' not in re.sub(r'\\.', '', bare), where  # an unescaped dot outside a class


def corpus():
    base = ['abc', 'docs/a.md', 'data/a.db', 'data/a/', 'id:ab', 'ab', 'docs/a/**', 'a b', 'Ґанок']
    odd = ['\n', '\r', NEL, LS, PS, BOM, chr(0xA0), chr(0x3000), chr(0x1F600), ' ', '\t', '.', '/', '..']
    out = list(base)
    for s in base:
        for c in odd:
            out += [s + c, c + s, s[:1] + c + s[1:]]
    out += [c * 3 for c in odd] + [BOM + BOM + BOM, LS + PS + ' ', NEL * 3, 'docs/../a.md', 'data/x/../y.db']
    return out


@pytest.mark.usefixtures('configured_roots')
def test_python_and_ecma_engines_agree_on_every_schema_pattern():
    node = shutil.which('node')
    if node is None:
        if os.environ.get('CI'):
            pytest.fail('node is required in CI to compare the ECMA-262 engine')
        pytest.skip('node is not installed; CI runs this comparison')
    patterns = [p for _, p in schema_patterns()]
    strings = corpus()
    script = ('const d=JSON.parse(require("fs").readFileSync(0,"utf8"));'
              'console.log(JSON.stringify(d.p.map(p=>{const r=new RegExp(p,"u");return d.s.map(s=>r.test(s));})));')
    payload = json.dumps({'p': patterns, 's': strings})
    ecma = json.loads(subprocess.run([node, '-e', script], input=payload, capture_output=True, text=True,
                                     check=True, timeout=60).stdout)
    script_plain = script.replace('new RegExp(p,"u")', 'new RegExp(p)')
    ecma_plain = json.loads(subprocess.run([node, '-e', script_plain], input=payload, capture_output=True,
                                           text=True, check=True, timeout=60).stdout)
    for p, js, js_plain in zip(patterns, ecma, ecma_plain, strict=True):
        py = [re.search(p, s) is not None for s in strings]
        assert py == js, [(s, a, b) for s, a, b in zip(strings, py, js, strict=True) if a != b][:5]
        assert py == js_plain, [(s, a, b) for s, a, b in zip(strings, py, js_plain, strict=True) if a != b][:5]
