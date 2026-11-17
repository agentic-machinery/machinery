"""Shipping gate regressions: a new unbounded JSON reader must fail CI."""

from pathlib import Path

import pytest

from lu_machinery.lint import json_readers


def _package(tmp_path: Path, source: str) -> Path:
    root = tmp_path / "lu_machinery"
    (root / "common").mkdir(parents=True)
    (root / "common/json_depth.py").write_text("import json\njson.loads('[]')\n", encoding="utf-8")
    (root / "reader.py").write_text(source, encoding="utf-8")
    return root


@pytest.mark.parametrize("source", [
    "import json\njson.loads(text)",
    "import json as j\nj.load(stream)",
    "from json import loads as read\nread(text)",
    "from json import load\nload(stream)",
    "import json\nread=json.loads\nread(text)",
    "import json\njson.JSONDecoder().raw_decode(text)",
    "import json\nreader=json.JSONDecoder()\nreader.raw_decode(text)",
    "from json import JSONDecoder as Decoder\nreader=Decoder()\nreader.decode(text)",
    "import json.decoder as d\nd.JSONDecoder().decode(text)",
    'import json\ngetattr(json,"loads")(text)',
    'script = "import json\\njson.loads(text)"',
    "import json\nself.d=json.JSONDecoder(); self.d.decode(s)",
    "import json\ndef r(s, decoder=json.JSONDecoder()): return decoder.decode(s)",
    "import json\nclass D(json.JSONDecoder): pass\nD().decode(s)",
    "import json\njson.JSONDecoder.decode(json.JSONDecoder(), s)",
    "import json\n(d := json.JSONDecoder()).decode(s)",
    "import json\nd,e=json.JSONDecoder(),1\nd.decode(s)",
    "import json\njson._default_decoder.decode(s)",
    "import json\njson.scanner.make_scanner(json.JSONDecoder())(s,0)",
])
def test_new_bare_reader_and_aliases_fail_the_gate(tmp_path, capsys, source):
    root = _package(tmp_path, source)
    assert json_readers.main(["--root", str(root)]) == 1
    assert "unbounded_json_reader" in capsys.readouterr().err


@pytest.mark.parametrize("source", [
    "from lu_machinery.common import json_depth\njson_depth.loads(text, max_depth=512)",
    "from lu_machinery.common import json_depth as j\nj.load(stream, max_depth=128)",
    "from lu_machinery.common.json_depth import raw_decode as read\nread(text, max_depth=512)",
    "from lu_machinery.common import json_depth\njson_depth.raw_decode(text, max_depth=512)",
    "import json\nfrom lu_machinery.common import json_depth\njson_depth.raw_decode(text, max_depth=512, decoder=json.JSONDecoder())",
    "import json\nfrom lu_machinery.common import json_depth\ndef r(text):\n d=json.JSONDecoder()\n return json_depth.raw_decode(text, max_depth=512, decoder=d)",
])
def test_bounded_readers_and_only_the_helper_are_admitted(tmp_path, capsys, source):
    # A real serializer helper name must not be confused with the stdlib C module.
    source += "\ndef _json(value): return value\n_json(1)\n"
    root = _package(tmp_path, source)
    assert json_readers.main(["--root", str(root)]) == 0
    assert "JSON reader lint passed" in capsys.readouterr().out
    (root / "json_depth.py").write_text("import json\njson.loads(text)", encoding="utf-8")
    assert json_readers.main(["--root", str(root)]) == 1
    assert "unbounded_json_reader" in capsys.readouterr().err


def test_unreadable_source_cannot_make_the_gate_green(tmp_path, monkeypatch, capsys):
    root = _package(tmp_path, "pass")
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == root / "reader.py":
            raise PermissionError("unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert json_readers.main(["--root", str(root)]) == 1
    assert "json_reader_scan_incomplete: PermissionError" in capsys.readouterr().err


def test_invalid_python_cannot_hide_a_bare_reader(tmp_path, capsys):
    root = _package(tmp_path, "import json\njson.loads(")
    assert json_readers.main(["--root", str(root)]) == 1
    assert "json_reader_scan_incomplete: SyntaxError" in capsys.readouterr().err
