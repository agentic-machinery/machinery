#!/usr/bin/env python3
"""Build the drill's public fixtures as small scrubbed extracts of a product checkout's real stores.

Run on a host that has the product checkout, never on the tester's machine:

    bash -c 'ulimit -v 2000000; python3 -I extract_fixtures.py --product-root /path/to/product'

Every store is opened read-only (SQLite ``mode=ro``; task records read as text) and nothing is
written back. The output goes to ``fixtures/`` beside this script and is committed with the kit.

Extraction rules (what is kept, what is dropped):

- ``vesum-sample.tsv``: for each lemma in ``LEMMAS`` (common public words), up to ``FORMS_PER_LEMMA``
  rows of word form, lemma, part of speech and tags from VESUM's ``forms_all`` table, read through
  the lemma index. Dropped: row ids, entry ids, source comments and source locations.
- ``sources-tools.json``: the names of the tools the Sources MCP server declares and whether each is
  read-only, parsed from ``server.py`` with ``ast`` (the file is never executed). Nothing else from
  the file is kept.
- ``task-records.json``: ``TASK_RECORDS`` terminal dispatcher records (two of them failed or done,
  schema version 2), reduced to ``TASK_FIELDS``: status, mode, agent and model. Dropped: task ids,
  prompts, paths, branches, accounts, PIDs, hosts, timestamps and every other field. The drill gives
  them synthetic ids, paths and timestamps when it builds its world.

The script refuses output that names a home directory, an e-mail address or a non-loopback IPv4
address. Credentials are never extracted; the drill uses fake ones.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent
LEMMAS = (
    "мова",
    "книга",
    "хата",
    "вода",
    "слово",
    "школа",
    "читати",
    "писати",
    "добрий",
    "день",
)
FORMS_PER_LEMMA = 40
TASK_RECORDS = 3
TASK_FIELDS = ("status", "mode", "agent", "model")
TERMINAL = ("done", "failed")
MAX_RECORD_BYTES = 256 * 1024
FORBIDDEN = re.compile(
    r"/(?:home|Users|root)/|[\w.+-]+@[\w-]+\.[\w.]+|\b(?!127\.0\.0\.1\b)(?:\d{1,3}\.){3}\d{1,3}\b"
)


def vesum_sample(db: Path) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for lemma in LEMMAS:
            cursor = connection.execute(
                "SELECT word_form, lemma, pos, tags FROM forms_all WHERE lemma = ? ORDER BY id LIMIT ?",
                (lemma, FORMS_PER_LEMMA),
            )
            rows.extend(cursor.fetchall())
    finally:
        connection.close()
    return rows


def sources_tools(server: Path) -> dict[str, list[str]]:
    tree = ast.parse(server.read_text(encoding="utf-8"))
    read_only_names = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", None) == "ToolAnnotations"
        ):
            hints = {kw.arg: kw.value for kw in node.value.keywords}
            hint = hints.get("readOnlyHint")
            if isinstance(hint, ast.Constant) and hint.value is True:
                read_only_names.update(
                    t.id for t in node.targets if isinstance(t, ast.Name)
                )
    helper = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_tool"
    )
    default = next(
        n.args[1].id
        for n in ast.walk(helper)
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", None) == "setdefault"
        and len(n.args) == 2
    )
    listing = next(
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "list_tools"
    )
    result: dict[str, list[str]] = {"read_only": [], "persisting": []}
    for node in ast.walk(listing):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_tool":
            fields = {kw.arg: kw.value for kw in node.keywords}
            name = fields.get("name")
            annotation = fields.get("annotations")
            label = annotation.id if isinstance(annotation, ast.Name) else default
            if isinstance(name, ast.Constant):
                result[
                    "read_only" if label in read_only_names else "persisting"
                ].append(name.value)
    return {key: sorted(set(value)) for key, value in result.items()}


def task_records(tasks: Path) -> list[dict[str, str]]:
    kept: list[dict[str, str]] = []
    names = sorted(
        entry.name
        for entry in os.scandir(tasks)
        if entry.name.endswith(".json") and entry.is_file()
    )
    for name in names:
        path = tasks / name
        if path.stat().st_size > MAX_RECORD_BYTES:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != 2
            or record.get("status") not in TERMINAL
        ):
            continue
        reduced = {field: record.get(field) for field in TASK_FIELDS}
        if all(isinstance(value, str) and value for value in reduced.values()):
            kept.append(reduced)
        if len(kept) == TASK_RECORDS:
            break
    if len(kept) < TASK_RECORDS:
        raise SystemExit(f"only {len(kept)} usable task records under {tasks.name}")
    return kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--product-root",
        required=True,
        type=Path,
        help="Product checkout holding the stores",
    )
    parser.add_argument(
        "--vesum",
        type=Path,
        help="VESUM SQLite file (default: <product-root>/data/vesum.db)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=KIT / "fixtures",
        help="Output directory (default: fixtures/)",
    )
    args = parser.parse_args(argv)
    root = args.product_root.resolve()
    outputs = {
        "vesum-sample.tsv": "".join(
            "\t".join(row) + "\n"
            for row in vesum_sample(
                (args.vesum or root / "data" / "vesum.db").resolve()
            )
        ),
        "sources-tools.json": json.dumps(
            sources_tools(root / ".mcp" / "servers" / "sources" / "server.py"), indent=1
        )
        + "\n",
        "task-records.json": json.dumps(
            task_records(root / "batch_state" / "tasks"), indent=1
        )
        + "\n",
    }
    for name, text in outputs.items():
        leak = FORBIDDEN.search(text)
        if leak:
            raise SystemExit(
                f"{name}: refused, output names a private detail ({leak.group(0)[:3]}...)"
            )
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in outputs.items():
        (args.out / name).write_text(text, encoding="utf-8")
        print(f"{name}: {len(text.encode())} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
