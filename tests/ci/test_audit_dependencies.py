from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from lu_machinery.ci.audit_dependencies import (
    audit_node,
    audit_python,
    filter_npm_audit_vulnerabilities,
    load_npm_audit_ignores,
    load_pip_audit_ignores,
    resolve_cve_to_ghsa,
)


def test_load_pip_audit_ignores(tmp_path: Path):
    ignore_file = tmp_path / ".pip-audit-ignore.yaml"
    data = {
        "vulnerabilities": [
            {"id": "CVE-2026-0001", "package": "foo", "reason": "test"},
            {"id": "PYSEC-2026-0002", "package": "bar", "reason": "test2"},
            "CVE-2026-0003",
        ]
    }
    ignore_file.write_text(yaml.safe_dump(data), encoding="utf-8")

    ignores = load_pip_audit_ignores(ignore_file)
    assert ignores == ["CVE-2026-0001", "PYSEC-2026-0002", "CVE-2026-0003"]


def test_load_npm_audit_ignores(tmp_path: Path):
    ignore_file = tmp_path / "npm-audit-ignore.yaml"
    data = {
        "vulnerabilities": [
            {"id": "GHSA-ch52-4w7c-c8xp", "package": "http-cache-semantics", "reason": "test"},
            {"cve": "CVE-2026-9999", "package": "foo", "reason": "test2"},
            {"package": "bar", "reason": "test3"},
            "GHSA-1111-2222-3333",
        ]
    }
    ignore_file.write_text(yaml.safe_dump(data), encoding="utf-8")

    ignores = load_npm_audit_ignores(ignore_file)
    assert "GHSA-CH52-4W7C-C8XP" in ignores
    assert "CVE-2026-9999" in ignores
    assert "bar" in ignores
    assert "GHSA-1111-2222-3333" in ignores
    assert "http-cache-semantics" not in ignores
    assert "foo" not in ignores


def test_filter_npm_audit_vulnerabilities_all_suppressed():
    vulns = {
        "@astrojs/mdx": {
            "name": "@astrojs/mdx",
            "severity": "high",
            "via": ["astro"],
        },
        "astro": {
            "name": "astro",
            "severity": "high",
            "via": ["http-cache-semantics"],
        },
        "http-cache-semantics": {
            "name": "http-cache-semantics",
            "severity": "high",
            "via": [
                {
                    "name": "http-cache-semantics",
                    "dependency": "http-cache-semantics",
                    "url": "https://github.com/advisories/GHSA-ch52-4w7c-c8xp",
                    "severity": "high",
                }
            ],
        },
    }
    suppressed, unsuppressed = filter_npm_audit_vulnerabilities(vulns, ["GHSA-CH52-4W7C-C8XP"])
    assert suppressed == {"@astrojs/mdx", "astro", "http-cache-semantics"}
    assert unsuppressed == {}


def test_filter_npm_audit_vulnerabilities_unsuppressed():
    vulns = {
        "astro": {
            "name": "astro",
            "severity": "high",
            "via": [
                "http-cache-semantics",
                {
                    "name": "astro",
                    "dependency": "astro",
                    "url": "https://github.com/advisories/GHSA-unsuppressed",
                    "severity": "high",
                },
            ],
        },
        "http-cache-semantics": {
            "name": "http-cache-semantics",
            "severity": "high",
            "via": [
                {
                    "name": "http-cache-semantics",
                    "dependency": "http-cache-semantics",
                    "url": "https://github.com/advisories/GHSA-ch52-4w7c-c8xp",
                    "severity": "high",
                }
            ],
        },
    }
    suppressed, unsuppressed = filter_npm_audit_vulnerabilities(vulns, ["GHSA-CH52-4W7C-C8XP"])
    assert suppressed == {"http-cache-semantics"}
    assert "astro" in unsuppressed


def test_audit_python_no_lockfile(tmp_path: Path):
    res = audit_python(tmp_path, tmp_path / "ignore.yaml")
    assert res == 1


@patch("subprocess.run")
def test_audit_python_success(mock_run: MagicMock, tmp_path: Path, monkeypatch):
    req_file = tmp_path / "requirements-lock.txt"
    req_file.write_text("package==1.0.0\n", encoding="utf-8")
    ignore_file = tmp_path / "ignore.yaml"
    ignore_file.write_text("vulnerabilities:\n  - id: CVE-TEST\n", encoding="utf-8")

    mock_run.return_value = MagicMock(returncode=0, stdout="No known vulnerabilities found", stderr="")

    res = audit_python(tmp_path, ignore_file)
    assert res == 0
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    kwargs = mock_run.call_args[1]
    assert "--ignore-vuln" in cmd
    assert "CVE-TEST" in cmd
    assert kwargs.get("timeout") == 300

    payload = b"verified wheel bytes"
    digest = hashlib.sha256(payload).hexdigest()
    filename = "lu_machinery-1.2.3-py3-none-any.whl"
    url = f"https://example.invalid/releases/download/v1.2.3/{filename}"
    wheel = tmp_path / filename
    wheel.write_bytes(payload)
    monkeypatch.setenv("LU_MACHINERY_WHEELHOUSE", str(tmp_path))
    runtime = tmp_path / "packages/runtime"
    runtime.mkdir(parents=True)
    (tmp_path / "uv.lock").write_text(
        'version = 1\n'
        '[[package]]\nname = "lu-machinery"\nversion = "1.2.3"\n'
        f'source = {{ url = "{url}" }}\nwheels = [{{ url = "{url}", hash = "sha256:{digest}" }}]\n'
        '[[package]]\nname = "runtime"\nsource = { directory = "packages/runtime" }\n'
        '[[package]]\nname = "git-dep"\n'
        f'source = {{ git = "https://example.invalid/git-dep?rev=main#{"a" * 40}" }}\n'
        '[[package]]\nname = "torch"\nversion = "1.2.3+cpu"\n'
        'source = { registry = "https://download.pytorch.org/whl/cpu" }\n'
        '[[package]]\nname = "package"\nversion = "1.0.0"\nsource = { registry = "https://pypi.org/simple" }\n'
        '[[package]]\nname = "package"\nversion = "2.0.0"\nsource = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("lu_machinery.ci.audit_dependencies.shutil.which", lambda _: "uv")

    incomplete_report = False

    def uv_or_audit(argv, **_kwargs):
        if argv[0] == "uv":
            assert argv[1:4] == ["export", "--locked", "--offline"]
            assert "--no-build" in argv
            assert {argv[i + 1] for i, arg in enumerate(argv) if arg == "--no-emit-package"} == {
                "git-dep", "lu-machinery", "runtime",
            }
            return MagicMock(returncode=0, stdout=(
                f"package==1.0.0 ; sys_platform != 'win32' --hash=sha256:{digest}\n"
                f"package==2.0.0 ; sys_platform == 'win32' --hash=sha256:{digest}\n"
                f"torch==1.2.3+cpu --hash=sha256:{digest}\n"
            ))
        assert {"--require-hashes", "--disable-pip", "--strict", "CVE-TEST"} <= set(argv)
        pins = Path(argv[argv.index("-r") + 1]).read_text(encoding="utf-8")
        if pins.startswith("package==1.0.0"):
            assert pins == f"package==1.0.0 --hash=sha256:{digest}\ntorch==1.2.3 --hash=sha256:{digest}\n"
            audited = [("package", "1.0.0"), ("torch", "1.2.3")]
        else:
            assert pins == f"package==2.0.0 --hash=sha256:{digest}\n"
            audited = [] if incomplete_report else [("package", "2.0.0")]
        return MagicMock(returncode=0, stdout=json.dumps({
            "dependencies": [{"name": name, "version": version, "vulns": []} for name, version in audited],
        }))

    mock_run.reset_mock()
    mock_run.side_effect = uv_or_audit
    assert audit_python(tmp_path, ignore_file) == 0
    assert mock_run.call_count == 3
    incomplete_report = True
    assert audit_python(tmp_path, ignore_file) == 1
    mock_run.reset_mock()
    wheel.write_bytes(b"tampered")
    assert audit_python(tmp_path, ignore_file) == 1
    mock_run.assert_not_called()
    wheel.write_bytes(payload)
    runtime.rmdir()
    runtime.symlink_to(runtime.name)
    assert audit_python(tmp_path, ignore_file) == 1
    mock_run.assert_not_called()


@patch("subprocess.run")
def test_audit_python_failure(mock_run: MagicMock, tmp_path: Path):
    req_file = tmp_path / "requirements-lock.txt"
    req_file.write_text("package==1.0.0\n", encoding="utf-8")
    ignore_file = tmp_path / "ignore.yaml"

    mock_run.return_value = MagicMock(returncode=1, stdout="Found 1 known vulnerability", stderr="")

    res = audit_python(tmp_path, ignore_file)
    assert res == 1


def test_lu_machinery_requirement_resolves_from_machinery_lock_not_pypi(tmp_path: Path, monkeypatch) -> None:
    """Resolve lu-machinery from the lock's wheel, not from PyPI.

    The fixture names lu-machinery only through ./packages/v4-runtime.
    lu-machinery-extras does not count. A wheel that is not the lock digest,
    and a declared wheelhouse without that wheel, return 1 and do not run pip-audit.
    Defect class: a lu-machinery dependency the requirements lines do not show
    (transitive) resolving from an index; when the machinery lock pins
    lu-machinery the constraint applies even if no line names it.
    """

    def write_pyproject(directory: Path, name: str, dependencies: list[str]) -> None:
        directory.mkdir(parents=True)
        rendered = ", ".join(repr(dependency) for dependency in dependencies)
        (directory / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "1.0.0"\ndependencies = [{rendered}]\n',
            encoding="utf-8",
        )

    root = tmp_path / "match"
    root.mkdir()
    write_pyproject(root / "packages" / "v4-runtime", "learn-ukrainian-v4-runtime", ["lu-machinery>=0.1.21"])
    write_pyproject(
        root / "packages" / "other-first-party",
        "other-first-party",
        ["requests", "lu-machinery-extras"],
    )
    req = root / "requirements-lock.txt"
    original = "requests==2.32.0\n./packages/v4-runtime\n./packages/other-first-party\n"
    req.write_text(original, encoding="utf-8")
    payload = b"fake-lu-machinery-9.9.9"
    digest = hashlib.sha256(payload).hexdigest()
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel_name = "lu_machinery-9.9.9-py3-none-any.whl"
    (wheelhouse / wheel_name).write_bytes(payload)
    (root / "requirements-machinery.lock").write_text(
        f"lu-machinery==9.9.9 \\\n    --hash=sha256:{digest}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LU_MACHINERY_WHEELHOUSE", str(wheelhouse))
    captured: dict[str, object] = {}

    def fake_run(_cmd: list[str], **kwargs: object) -> MagicMock:
        env = kwargs["env"]
        assert isinstance(env, dict)
        links = Path(env["PIP_FIND_LINKS"])
        staged = list(links.iterdir())
        assert staged == [links / wheel_name]
        captured["wheel_hash"] = hashlib.sha256(staged[0].read_bytes()).hexdigest()
        captured["constraint"] = Path(env["PIP_CONSTRAINT"]).read_text(encoding="utf-8")
        # pip 26.2+ isolated builds read only build constraints.
        assert env["PIP_BUILD_CONSTRAINT"] == env["PIP_CONSTRAINT"]
        captured["cmd"] = _cmd
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        assert audit_python(root, root / "ignore.yaml") == 0
    mock_run.assert_called_once()
    assert req.read_text(encoding="utf-8") == original
    constraint = captured["constraint"]
    assert isinstance(constraint, str)
    assert constraint.strip().startswith("lu-machinery @ file:")
    assert wheel_name in constraint
    assert "--hash" not in constraint
    assert captured["wheel_hash"] == digest
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[1:5] == ["-m", "pip_audit", "-r", str(req)]

    extras = tmp_path / "extras"
    extras.mkdir()
    write_pyproject(
        extras / "packages" / "other-first-party",
        "other-first-party",
        ["requests", "lu-machinery-extras"],
    )
    (extras / "requirements-lock.txt").write_text("./packages/other-first-party\n", encoding="utf-8")
    monkeypatch.delenv("LU_MACHINERY_WHEELHOUSE", raising=False)

    def fake_unset(_cmd: list[str], **kwargs: object) -> MagicMock:
        captured["extras_env"] = kwargs.get("env")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_unset):
        assert audit_python(extras, extras / "ignore.yaml") == 0
    assert captured["extras_env"] is None
    assert not (extras / "requirements-machinery.lock").exists()

    mismatch = tmp_path / "mismatch"
    mismatch.mkdir()
    write_pyproject(mismatch / "packages" / "v4-runtime", "learn-ukrainian-v4-runtime", ["lu-machinery>=0.1.21"])
    (mismatch / "requirements-lock.txt").write_text("./packages/v4-runtime\n", encoding="utf-8")
    (mismatch / "requirements-machinery.lock").write_text(
        "lu-machinery==9.9.9 \\\n    --hash=sha256:" + ("ab" * 32) + "\n",
        encoding="utf-8",
    )
    bad_house = tmp_path / "bad-wheelhouse"
    bad_house.mkdir()
    (bad_house / wheel_name).write_bytes(b"not-the-pinned-wheel")
    monkeypatch.setenv("LU_MACHINERY_WHEELHOUSE", str(bad_house))
    with patch("subprocess.run") as mismatch_run:
        assert audit_python(mismatch, mismatch / "ignore.yaml") == 1
    mismatch_run.assert_not_called()

    empty_house = tmp_path / "empty-wheelhouse"
    empty_house.mkdir()
    monkeypatch.setenv("LU_MACHINERY_WHEELHOUSE", str(empty_house))
    with patch("subprocess.run") as missing_run:
        assert audit_python(mismatch, mismatch / "ignore.yaml") == 1
    missing_run.assert_not_called()

    # Forms pip accepts must reach the wheel check, not skip it.
    spelled = tmp_path / "spelled"
    spelled.mkdir()
    runtime = spelled / "packages" / "runtime"
    write_pyproject(runtime, "demo-runtime", ["lu-machinery>=0.1.21"])
    runtime_uri = runtime.resolve().as_uri()
    monkeypatch.delenv("LU_MACHINERY_WHEELHOUSE", raising=False)
    for spelled_line in (
        "packages/runtime",
        "./packages/runtime[extra]",
        runtime_uri,
        f"demo-runtime @ {runtime_uri}",
        "-e./packages/runtime",
        "-epackages/runtime",
        "-epackages/runtime[extra]",
        f"-e{runtime.resolve()}",
        f"-e{runtime_uri}",
        "lu-machinery(>=0.1.21)",
    ):
        (spelled / "requirements-lock.txt").write_text(spelled_line + "\n", encoding="utf-8")
        with patch("subprocess.run") as spelled_run:
            assert audit_python(spelled, spelled / "ignore.yaml") == 1, spelled_line
        spelled_run.assert_not_called()

    # The machinery lock pins lu-machinery and no line names it: a pinned package
    # or a commit-pinned git reference can still pull it in, so the constraint applies.
    transitive = tmp_path / "transitive"
    transitive.mkdir()
    (transitive / "requirements-machinery.lock").write_text(
        f"lu-machinery==9.9.9 \\\n    --hash=sha256:{digest}\n", encoding="utf-8"
    )
    (transitive / "requirements-lock.txt").write_text(
        "requests==2.32.0 \\\n    --hash=sha256:" + ("cd" * 32) + "\n"
        "ipa-uk @ git+https://github.com/lang-uk/ipa-uk.git@e7fc944faca090ca985d5ca09a24c59154d9dea1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LU_MACHINERY_WHEELHOUSE", str(wheelhouse))
    captured.clear()
    with patch("subprocess.run", side_effect=fake_run) as transitive_run:
        assert audit_python(transitive, transitive / "ignore.yaml") == 0
    transitive_run.assert_called_once()
    assert captured["wheel_hash"] == digest
    assert str(captured["constraint"]).startswith("lu-machinery @ file:")


_SHADOWED_GIT_REFERENCE = "shadow @ git+https://example.invalid/shadow.git@" + "a" * 40
_REFUSED_REQUIREMENT_FORMS = (
    "-eruntime",
    "-eruntime[extra]",
    "-e runtime",
    "-e runtime[extra]",
    "--editable runtime",
    "--editable runtime[extra]",
    "--editable=runtime",
    "--editable=runtime[extra]",
    "-e ./runtime",
    "runtime",
    "runtime/",
    "./runtime ; python_version > '3'",
    "./empty",
    "file:runtime",
    "demo-runtime @ file:runtime",
    "https://example.invalid/demo-1.0-py3-none-any.whl",
    "demo @ https://example.invalid/demo-1.0-py3-none-any.whl",
    "git+https://example.invalid/demo.git#egg=demo",
    "demo @ git+https://example.invalid/demo.git@main",
    "lu-machinery @ git+https://example.invalid/lu-machinery.git@" + "0" * 40,
    "-r other.txt",
    "-c other.txt",
    "--index-url https://example.invalid/simple",
    "--extra-index-url https://example.invalid/simple",
    "-f ./wheels",
    "requests",
    "requests>=2.0",
    "requests==2.*",
    "requests==2.32.0 --config-settings=x=y",
    "requests==1.0+x.whl",
    "${RUNTIME}",
    "demo===1.0",
    "safe===x/runtime",
    _SHADOWED_GIT_REFERENCE,
)


@pytest.mark.parametrize("line", _REFUSED_REQUIREMENT_FORMS)
def test_unrecognized_requirement_line_fails_closed(line: str, tmp_path: Path, monkeypatch, capsys) -> None:
    """Any line that is not a pinned name, a commit-pinned git reference or a ./dir project returns 1.

    Defect class: a requirement spelling the classifier does not recognize being
    read as "does not need lu-machinery" instead of refusing the audit. The wheel
    and pin are valid, so only the line itself can stop pip-audit.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "pyproject.toml").write_text(
        '[project]\nname = "demo-runtime"\nversion = "1.0.0"\ndependencies = ["lu-machinery>=0.1.21"]\n',
        encoding="utf-8",
    )
    (tmp_path / "empty").mkdir()
    for shadow in (tmp_path / "safe===x" / "runtime", tmp_path / _SHADOWED_GIT_REFERENCE):
        # pip installs an existing directory spelled like the requirement instead.
        shadow.mkdir(parents=True)
        (shadow / "pyproject.toml").write_bytes((runtime / "pyproject.toml").read_bytes())
    payload = b"fake-lu-machinery-9.9.9"
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "lu_machinery-9.9.9-py3-none-any.whl").write_bytes(payload)
    (tmp_path / "requirements-machinery.lock").write_text(
        f"lu-machinery==9.9.9 --hash=sha256:{hashlib.sha256(payload).hexdigest()}\n", encoding="utf-8"
    )
    (tmp_path / "requirements-lock.txt").write_text(f"requests==2.32.0\n{line}\n", encoding="utf-8")
    monkeypatch.setenv("LU_MACHINERY_WHEELHOUSE", str(wheelhouse))
    with patch("subprocess.run") as refused_run:
        assert audit_python(tmp_path, tmp_path / "ignore.yaml") == 1
    refused_run.assert_not_called()
    assert "requirements-lock.txt line 2:" in capsys.readouterr().err


@patch("subprocess.run")
def test_audit_node_success(mock_run: MagicMock, tmp_path: Path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "package.json").write_text("{}", encoding="utf-8")
    (site_dir / "package-lock.json").write_text("{}", encoding="utf-8")

    mock_run.return_value = MagicMock(returncode=0, stdout="found 0 vulnerabilities", stderr="")

    res = audit_node(tmp_path)
    assert res == 0
    assert mock_run.call_count == 2
    for call_item in mock_run.call_args_list:
        cmd = call_item[0][0]
        kwargs = call_item[1]
        assert cmd == ["npm", "audit", "--omit=dev", "--audit-level=high", "--json"]
        assert kwargs.get("timeout") == 120


@patch("subprocess.run")
def test_audit_node_suppressed(mock_run: MagicMock, tmp_path: Path):
    import json

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "package.json").write_text("{}", encoding="utf-8")
    (site_dir / "package-lock.json").write_text("{}", encoding="utf-8")

    ignore_file = tmp_path / "npm-audit-ignore.yaml"
    ignore_file.write_text(
        "vulnerabilities:\n  - id: GHSA-ch52-4w7c-c8xp\n    package: http-cache-semantics\n",
        encoding="utf-8",
    )

    audit_json = json.dumps(
        {
            "vulnerabilities": {
                "http-cache-semantics": {
                    "name": "http-cache-semantics",
                    "severity": "high",
                    "via": [
                        {
                            "name": "http-cache-semantics",
                            "url": "https://github.com/advisories/GHSA-ch52-4w7c-c8xp",
                            "severity": "high",
                        }
                    ],
                }
            }
        }
    )

    mock_run.return_value = MagicMock(returncode=1, stdout=audit_json, stderr="")

    res = audit_node(tmp_path, ignore_file)
    assert res == 0


@patch("subprocess.run")
def test_audit_node_unsuppressed_fails(mock_run: MagicMock, tmp_path: Path):
    import json

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "package.json").write_text("{}", encoding="utf-8")
    (site_dir / "package-lock.json").write_text("{}", encoding="utf-8")

    audit_json = json.dumps(
        {
            "vulnerabilities": {
                "unsuppressed-pkg": {
                    "name": "unsuppressed-pkg",
                    "severity": "high",
                    "via": [
                        {
                            "name": "unsuppressed-pkg",
                            "url": "https://github.com/advisories/GHSA-xxxx-yyyy-zzzz",
                            "severity": "high",
                        }
                    ],
                }
            }
        }
    )

    mock_run.return_value = MagicMock(returncode=1, stdout=audit_json, stderr="")

    res = audit_node(tmp_path)
    assert res == 1


@patch("subprocess.run")
def test_audit_node_unparseable_json_fails(mock_run: MagicMock, tmp_path: Path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "package.json").write_text("{}", encoding="utf-8")
    (site_dir / "package-lock.json").write_text("{}", encoding="utf-8")

    mock_run.return_value = MagicMock(returncode=1, stdout="not json", stderr="fatal error")

    res = audit_node(tmp_path)
    assert res == 1


@patch("subprocess.run")
def test_audit_node_empty_vulns_with_nonzero_exit_fails(mock_run: MagicMock, tmp_path: Path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "package.json").write_text("{}", encoding="utf-8")
    (site_dir / "package-lock.json").write_text("{}", encoding="utf-8")

    mock_run.return_value = MagicMock(returncode=1, stdout='{"vulnerabilities": {}}', stderr="")

    res = audit_node(tmp_path)
    assert res == 1


@patch("subprocess.run")
def test_audit_node_suppressed_high_with_unrelated_moderate_passes(mock_run: MagicMock, tmp_path: Path):
    import json

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "package.json").write_text("{}", encoding="utf-8")
    (site_dir / "package-lock.json").write_text("{}", encoding="utf-8")

    ignore_file = tmp_path / "npm-audit-ignore.yaml"
    ignore_file.write_text(
        "vulnerabilities:\n  - id: GHSA-ch52-4w7c-c8xp\n    package: http-cache-semantics\n",
        encoding="utf-8",
    )

    audit_json = json.dumps(
        {
            "vulnerabilities": {
                "http-cache-semantics": {
                    "name": "http-cache-semantics",
                    "severity": "high",
                    "via": [
                        {
                            "name": "http-cache-semantics",
                            "url": "https://github.com/advisories/GHSA-ch52-4w7c-c8xp",
                            "severity": "high",
                        }
                    ],
                },
                "unrelated-moderate": {
                    "name": "unrelated-moderate",
                    "severity": "moderate",
                    "via": [
                        {
                            "name": "unrelated-moderate",
                            "url": "https://github.com/advisories/GHSA-moderate-advisory",
                            "severity": "moderate",
                        }
                    ],
                },
            }
        }
    )

    mock_run.return_value = MagicMock(returncode=1, stdout=audit_json, stderr="")

    res = audit_node(tmp_path, ignore_file)
    assert res == 0


@patch("subprocess.run")
def test_audit_node_suppressed_high_with_unrelated_critical_fails(mock_run: MagicMock, tmp_path: Path):
    import json

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "package.json").write_text("{}", encoding="utf-8")
    (site_dir / "package-lock.json").write_text("{}", encoding="utf-8")

    ignore_file = tmp_path / "npm-audit-ignore.yaml"
    ignore_file.write_text(
        "vulnerabilities:\n  - id: GHSA-ch52-4w7c-c8xp\n    package: http-cache-semantics\n",
        encoding="utf-8",
    )

    audit_json = json.dumps(
        {
            "vulnerabilities": {
                "http-cache-semantics": {
                    "name": "http-cache-semantics",
                    "severity": "high",
                    "via": [
                        {
                            "name": "http-cache-semantics",
                            "url": "https://github.com/advisories/GHSA-ch52-4w7c-c8xp",
                            "severity": "high",
                        }
                    ],
                },
                "unrelated-critical": {
                    "name": "unrelated-critical",
                    "severity": "critical",
                    "via": [
                        {
                            "name": "unrelated-critical",
                            "url": "https://github.com/advisories/GHSA-critical-advisory",
                            "severity": "critical",
                        }
                    ],
                },
            }
        }
    )

    mock_run.return_value = MagicMock(returncode=1, stdout=audit_json, stderr="")

    res = audit_node(tmp_path, ignore_file)
    assert res == 1


@patch("subprocess.run")
def test_audit_node_cve_suppression_does_not_suppress_unrelated_critical(mock_run: MagicMock, tmp_path: Path):
    import json

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "package.json").write_text("{}", encoding="utf-8")
    (site_dir / "package-lock.json").write_text("{}", encoding="utf-8")

    ignore_file = tmp_path / "npm-audit-ignore.yaml"
    ignore_file.write_text(
        "vulnerabilities:\n  - cve: CVE-2026-9999\n    package: foo-pkg\n",
        encoding="utf-8",
    )

    audit_json = json.dumps(
        {
            "vulnerabilities": {
                "foo-pkg": {
                    "name": "foo-pkg",
                    "severity": "critical",
                    "via": [
                        {
                            "name": "foo-pkg",
                            "url": "https://github.com/advisories/GHSA-unrelated-critical",
                            "severity": "critical",
                        }
                    ],
                }
            }
        }
    )

    mock_run.return_value = MagicMock(returncode=1, stdout=audit_json, stderr="")

    res = audit_node(tmp_path, ignore_file)
    assert res == 1


def test_resolve_cve_to_ghsa():
    # Known alias from static table
    assert resolve_cve_to_ghsa("CVE-2026-93748") == "GHSA-CH52-4W7C-C8XP"
    assert resolve_cve_to_ghsa("cve-2026-93748") == "GHSA-CH52-4W7C-C8XP"

    # Custom aliases dictionary
    custom = {"CVE-9999-1111": "GHSA-CUSTOM-ID"}
    assert resolve_cve_to_ghsa("CVE-9999-1111", custom) == "GHSA-CUSTOM-ID"

    # Unknown CVE returns None when network fails/is mocked
    with patch("urllib.request.urlopen", side_effect=Exception("network offline")):
        assert resolve_cve_to_ghsa("CVE-0000-0000") is None


def test_load_npm_audit_ignores_with_cve_aliases(tmp_path: Path):
    ignore_file = tmp_path / "npm-audit-ignore.yaml"
    data = {
        "aliases": {
            "CVE-1111-2222": "GHSA-custom-alias",
        },
        "vulnerabilities": [
            {"cve": "CVE-2026-93748", "package": "http-cache-semantics"},
            {"cve": "CVE-1111-2222", "package": "other-pkg"},
        ],
    }
    ignore_file.write_text(yaml.safe_dump(data), encoding="utf-8")

    ignores = load_npm_audit_ignores(ignore_file)
    assert "CVE-2026-93748" in ignores
    assert "GHSA-CH52-4W7C-C8XP" in ignores
    assert "CVE-1111-2222" in ignores
    assert "GHSA-CUSTOM-ALIAS" in ignores
    assert "http-cache-semantics" not in ignores


def test_filter_npm_audit_vulnerabilities_cve_resolves_to_ghsa():
    vulns = {
        "http-cache-semantics": {
            "name": "http-cache-semantics",
            "severity": "high",
            "via": [
                {
                    "name": "http-cache-semantics",
                    "dependency": "http-cache-semantics",
                    "url": "https://github.com/advisories/GHSA-ch52-4w7c-c8xp",
                    "severity": "high",
                }
            ],
        },
    }
    suppressed, unsuppressed = filter_npm_audit_vulnerabilities(vulns, ["CVE-2026-93748"])
    assert suppressed == {"http-cache-semantics"}
    assert unsuppressed == {}


@patch("subprocess.run")
def test_audit_node_cve_suppression_successfully_suppresses_intended_canonical_advisory(
    mock_run: MagicMock, tmp_path: Path
):
    import json

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "package.json").write_text("{}", encoding="utf-8")
    (site_dir / "package-lock.json").write_text("{}", encoding="utf-8")

    ignore_file = tmp_path / "npm-audit-ignore.yaml"
    ignore_file.write_text(
        "vulnerabilities:\n  - cve: CVE-2026-93748\n    package: http-cache-semantics\n",
        encoding="utf-8",
    )

    audit_json = json.dumps(
        {
            "vulnerabilities": {
                "http-cache-semantics": {
                    "name": "http-cache-semantics",
                    "severity": "high",
                    "via": [
                        {
                            "name": "http-cache-semantics",
                            "url": "https://github.com/advisories/GHSA-ch52-4w7c-c8xp",
                            "severity": "high",
                        }
                    ],
                }
            }
        }
    )

    mock_run.return_value = MagicMock(returncode=1, stdout=audit_json, stderr="")

    res = audit_node(tmp_path, ignore_file)
    assert res == 0
