"""One smoke command for the Monitor API, the Sources server, and pinned host units.

``api`` GETs every ``all_page_contracts()`` URL and every parameter-free GET
covered by an HTTP route contract. Each must answer within the budget with no
5xx. ``sources`` checks the Sources server the host unit actually runs
(loopback ``/health``, then one ``verify_word``). ``host`` requires every unit
named by ``Requires=`` in ``packaging/systemd/learn-ukrainian-loopback.target``
to report ``active``.

The JSON document on stdout is the receipt. It records status, elapsed time,
and a short detail. It does not record response bodies.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_MACHINERY = Path(os.path.abspath(__file__)).parents[1].name
AREAS = ("api", "sources", "host")
DEFAULT_MONITOR = "http://127.0.0.1:8765"
DEFAULT_SOURCES = "http://127.0.0.1:8766"
DEFAULT_WORD = "мова"
DEFAULT_BUDGET_S = 1.0
# A cold verify_word on the live server takes several seconds. The 1s budget
# is for /health and for Monitor GETs; the tool call has its own ceiling.
TOOL_TIMEOUT_S = 20.0
JSON_LIMIT = 2_000_000
TOOL_LIMIT = 65_536
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_UNIT = re.compile(r"^[a-zA-Z0-9@_.-]+\.service$")
_UNIT_STATES = frozenset(
    {"active", "inactive", "failed", "activating", "deactivating", "reloading"}
)
_TARGET_NAME = "learn-ukrainian-loopback.target"


class VerifyFailure(Exception):
    """One check could not be set up. ``detail`` is safe to put in the receipt.

    ``check`` is the measured check when the failure is itself a request, so the
    receipt keeps that target, status, and elapsed time.
    """

    def __init__(self, detail: str, *, check: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.check = check


def select_api_targets(
    page_urls: Sequence[str], contracts: Sequence[Any], get_paths: Sequence[str]
) -> list[str]:
    """Page URLs, each concrete HTTP contract path, and parameter-free GETs it covers.

    A contract whose pattern contains ``{`` is not a URL we can request. Prefix
    contracts still contribute the parameter-free GET paths OpenAPI lists under
    them, which is how ``/api/dashboard/pipeline`` is checked.
    """
    concrete = [
        contract
        for contract in contracts
        if getattr(contract, "kind", None) == "http"
        and "{" not in str(getattr(contract, "pattern", ""))
    ]
    chosen: set[str] = set()
    for url in page_urls:
        if isinstance(url, str) and url.startswith("/") and "{" not in url:
            chosen.add(url)
    for contract in concrete:
        pattern = str(contract.pattern)
        if pattern.startswith("/") and pattern != "/{path:path}":
            chosen.add(pattern)
    for path in get_paths:
        if not isinstance(path, str) or not path.startswith("/") or "{" in path:
            continue
        if any(contract.matches(path, "http") for contract in concrete):
            chosen.add(path)
    return sorted(chosen)


def _product_route_file() -> Path | None:
    """The product route-contract module, when this host has the product checkout."""
    from lu_machinery.common.repo_root import (
        PRIMARY_ROOT_ENV,
        PRODUCT_ROOT_ENV,
        ProductRootError,
        product_root,
    )

    candidates: list[Path] = []
    configured = os.environ.get(PRODUCT_ROOT_ENV, "")
    if configured:
        candidates.append(Path(configured))
    with contextlib.suppress(ProductRootError):
        candidates.append(product_root())
    primary = os.environ.get(PRIMARY_ROOT_ENV, "")
    if primary:
        candidates.append(Path(primary))
    seen: set[Path] = set()
    for root in candidates:
        if root in seen:
            continue
        seen.add(root)
        path = root / "scripts" / "project_api" / "route_contracts.py"
        if path.is_file():
            return path
    return None


def _load_product_routes() -> Any | None:
    path = _product_route_file()
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location("_verify_run_product_routes", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "ROUTE_CONTRACTS") or not hasattr(module, "PAGE_CONTRACTS"):
        return None
    return module


def joined_contracts() -> tuple[list[str], list[Any], bool]:
    """Machinery contracts plus the product's, and whether the product module loaded."""
    from lu_machinery.api.product_contribution import ProductContribution
    from lu_machinery.api.route_contracts import all_page_contracts, all_route_contracts

    module = _load_product_routes()
    if module is None:
        product = ProductContribution()
        loaded = False
    else:
        product = ProductContribution(
            route_contracts=tuple(module.ROUTE_CONTRACTS),
            page_contracts=tuple(module.PAGE_CONTRACTS),
        )
        loaded = True
    pages = [page.url for page in all_page_contracts(product)]
    return pages, list(all_route_contracts(product)), loaded


def _require_loopback(url: str) -> tuple[str, int, str]:
    parts = urlsplit(url)
    host = parts.hostname
    if (
        parts.scheme != "http"
        or host not in _LOOPBACK
        or parts.username
        or parts.password
    ):
        raise VerifyFailure("URL must be an http loopback origin")
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return host, port, path


def _exchange(
    url: str,
    *,
    method: str,
    timeout: float,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    read_limit: int = 0,
) -> tuple[int | None, bytes, float, str]:
    """Return status, a capped body, elapsed seconds, and a closed detail.

    ``detail`` is empty when a status line arrived. It never includes a path,
    a body, or an exception string.
    """
    host, port, path = _require_loopback(url)
    started = time.monotonic()
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        status = response.status
        payload = response.read(read_limit + 1) if read_limit else b""
        if read_limit and len(payload) > read_limit:
            return status, b"", time.monotonic() - started, "response too large"
        return status, payload, time.monotonic() - started, ""
    except TimeoutError:
        return None, b"", time.monotonic() - started, "timed out"
    except (OSError, http.client.HTTPException):
        elapsed = time.monotonic() - started
        detail = "timed out" if elapsed >= timeout else "connection failed"
        return None, b"", elapsed, detail
    finally:
        connection.close()


def _judge(
    status: int | None, elapsed: float, detail: str, *, budget: float
) -> tuple[bool, str]:
    if status is None:
        return False, detail or "connection failed"
    if detail == "response too large":
        return False, detail
    if status >= 500:
        return False, f"HTTP {status}"
    if elapsed >= budget:
        return False, f"slower than {budget:g}s"
    return True, ""


def _check(
    target: str, *, ok: bool, status: int | None, elapsed: float, detail: str
) -> dict[str, Any]:
    return {
        "target": target,
        "ok": ok,
        "status": status,
        "elapsed_s": round(elapsed, 3),
        "detail": detail,
    }


def check_get(base_url: str, path: str, *, budget: float) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    headers = {"Connection": "close", "Accept": "application/json, text/html;q=0.9"}
    status, _payload, elapsed, detail = _exchange(
        url, method="GET", timeout=budget, headers=headers
    )
    ok, detail = _judge(status, elapsed, detail, budget=budget)
    return _check(path, ok=ok, status=status, elapsed=elapsed, detail=detail)


def discover_api_paths(
    base_url: str, *, budget: float = DEFAULT_BUDGET_S
) -> tuple[list[str], bool]:
    """Return the GET targets and whether the product route contracts were loaded."""
    pages, contracts, loaded = joined_contracts()
    status, payload, elapsed, detail = _exchange(
        base_url.rstrip("/") + "/openapi.json",
        method="GET",
        timeout=budget,
        headers={"Connection": "close", "Accept": "application/json"},
        read_limit=JSON_LIMIT,
    )
    ok, detail = _judge(status, elapsed, detail, budget=budget)
    if not ok or status != 200:
        reason = detail or "openapi.json did not answer"
        raise VerifyFailure(reason, check=_openapi_check(status, elapsed, reason))
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerifyFailure(
            "openapi.json was not JSON",
            check=_openapi_check(status, elapsed, "openapi.json was not JSON"),
        ) from exc
    paths = document.get("paths") if isinstance(document, dict) else None
    if not isinstance(paths, dict):
        raise VerifyFailure(
            "openapi.json has no paths",
            check=_openapi_check(status, elapsed, "openapi.json has no paths"),
        )
    gets = [
        path for path, item in paths.items() if isinstance(item, dict) and "get" in item
    ]
    targets = select_api_targets(pages, contracts, gets)
    if not targets:
        raise VerifyFailure("no routes to check")
    return targets, loaded


def _wait_for_worker(base_url: str, *, budget: float) -> bool:
    """Wait until ``/`` answers inside the budget.

    Monitor runs one worker. A GET that the client abandons at 1s keeps running
    on that worker, so the next route would be timed while it is only queued.
    This wait is not a check. It returns False when the worker never frees.
    """
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status, _payload, elapsed, _detail = _exchange(
            base_url.rstrip("/") + "/",
            method="GET",
            timeout=budget,
            headers={"Connection": "close", "Accept": "text/html"},
        )
        if status is not None and elapsed < budget:
            return True
    return False


def run_api(
    base_url: str, paths: Sequence[str], *, budget: float, product_contracts: bool
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        item = check_get(base_url, path, budget=budget)
        checks.append(item)
        if item["status"] is not None:
            continue
        if _wait_for_worker(base_url, budget=budget):
            continue
        for rest in paths[index + 1 :]:
            checks.append(
                _check(
                    rest,
                    ok=False,
                    status=None,
                    elapsed=0.0,
                    detail="not checked; server did not recover",
                )
            )
        break
    return _area(
        "api",
        checks,
        base_url=base_url,
        budget_s=budget,
        product_contracts=product_contracts,
    )


def _health_ok(status: int | None, payload: bytes) -> bool:
    if status != 200:
        return False
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(document, dict) and document.get("status") == "ok"


def _verify_word_ok(payload: bytes) -> bool:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    result = document.get("result") if isinstance(document, dict) else None
    if not isinstance(result, dict) or result.get("isError") is True:
        return False
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        return False
    text = content[0].get("text")
    return isinstance(text, str) and "matches in VESUM" in text


def run_sources(base_url: str, word: str, *, budget: float) -> dict[str, Any]:
    """Probe the running Sources server. A new stdio process would hide a down unit."""
    health_url = base_url.rstrip("/") + "/health"
    status, payload, elapsed, detail = _exchange(
        health_url,
        method="GET",
        timeout=budget,
        headers={"Connection": "close", "Accept": "application/json"},
        read_limit=4096,
    )
    ok, detail = _judge(status, elapsed, detail, budget=budget)
    if ok and not _health_ok(status, payload):
        ok, detail = False, "health not ok"
    checks = [_check("/health", ok=ok, status=status, elapsed=elapsed, detail=detail)]
    if status is None:
        checks.append(
            _check(
                "verify_word",
                ok=False,
                status=None,
                elapsed=0.0,
                detail="skipped because /health did not answer",
            )
        )
        return _area("sources", checks, base_url=base_url, budget_s=budget, word=word)
    request = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "verify_word", "arguments": {"word": word}},
        }
    ).encode()
    headers = {
        "Connection": "close",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    tool_status, tool_body, tool_elapsed, tool_detail = _exchange(
        base_url.rstrip("/") + "/mcp",
        method="POST",
        timeout=TOOL_TIMEOUT_S,
        body=request,
        headers=headers,
        read_limit=TOOL_LIMIT,
    )
    tool_ok, tool_detail = _judge(
        tool_status, tool_elapsed, tool_detail, budget=TOOL_TIMEOUT_S
    )
    if tool_ok and not _verify_word_ok(tool_body):
        tool_ok, tool_detail = False, "verify_word failed"
    checks.append(
        _check(
            "verify_word",
            ok=tool_ok,
            status=tool_status,
            elapsed=tool_elapsed,
            detail=tool_detail,
        )
    )
    return _area("sources", checks, base_url=base_url, budget_s=budget, word=word)


def pinned_units(text: str) -> list[str]:
    """Service names in ``Requires=`` of the loopback target. That line is the pin."""
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("Requires="):
            continue
        for part in line.split("=", 1)[1].split():
            if _UNIT.fullmatch(part) and part not in names:
                names.append(part)
    if not names:
        raise VerifyFailure("loopback target names no units")
    return names


def _loopback_text() -> str:
    path = (
        Path(os.path.abspath(__file__)).parents[2]
        / "packaging"
        / "systemd"
        / _TARGET_NAME
    )
    if not path.is_file():
        raise VerifyFailure("loopback target unreadable")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VerifyFailure("loopback target unreadable") from exc
    if len(text) > 65_536:
        raise VerifyFailure("loopback target unreadable")
    return text


def run_host() -> dict[str, Any]:
    from lu_machinery.orchestration.dispatch_isolation import resolve_user_bus

    units = pinned_units(_loopback_text())
    bus = resolve_user_bus(os.environ)
    if bus.unavailable is not None and "XDG_RUNTIME_DIR" not in os.environ:
        checks = [
            _check(
                unit, ok=False, status=None, elapsed=0.0, detail="user bus unavailable"
            )
            for unit in units
        ]
        return _area("host", checks)
    checks = []
    for unit in units:
        started = time.monotonic()
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                capture_output=True,
                text=True,
                env=bus.env,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            checks.append(
                _check(
                    unit,
                    ok=False,
                    status=None,
                    elapsed=time.monotonic() - started,
                    detail="not active",
                )
            )
            continue
        state = result.stdout.strip()
        elapsed = time.monotonic() - started
        if result.returncode == 0 and state == "active":
            checks.append(_check(unit, ok=True, status=0, elapsed=elapsed, detail=""))
        else:
            detail = state if state in _UNIT_STATES else "not active"
            checks.append(
                _check(
                    unit,
                    ok=False,
                    status=result.returncode,
                    elapsed=elapsed,
                    detail=detail,
                )
            )
    return _area("host", checks)


def _area(name: str, checks: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    failed = [item["target"] for item in checks if not item["ok"]]
    body: dict[str, Any] = {
        "area": name,
        "ok": not failed,
        "passed": len(checks) - len(failed),
        "failed": failed,
        "checks": checks,
    }
    body.update(extra)
    return body


def _openapi_check(status: int | None, elapsed: float, detail: str) -> dict[str, Any]:
    return _check("/openapi.json", ok=False, status=status, elapsed=elapsed, detail=detail)


def _failed_area(
    name: str, detail: str, *, check: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    item = check or _check(name, ok=False, status=None, elapsed=0.0, detail=detail)
    return _area(name, [item], **extra)


def build_receipt(areas: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "verify-run.v1",
        "ok": all(area["ok"] for area in areas),
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "areas": areas,
    }


def run(
    areas: Sequence[str],
    *,
    base_url: str = DEFAULT_MONITOR,
    sources_url: str = DEFAULT_SOURCES,
    word: str = DEFAULT_WORD,
    budget: float = DEFAULT_BUDGET_S,
    api_paths: Sequence[str] | None = None,
    product_contracts: bool | None = None,
) -> dict[str, Any]:
    """Run the named areas and return the receipt. The first setup error fails that area only."""
    unknown = [area for area in areas if area not in AREAS]
    if unknown or not areas:
        raise VerifyFailure("area must be api, sources, or host")
    results: list[dict[str, Any]] = []
    for area in areas:
        if area == "api":
            try:
                if api_paths is None:
                    paths, loaded = discover_api_paths(base_url, budget=budget)
                else:
                    paths, loaded = list(api_paths), bool(product_contracts)
                results.append(
                    run_api(base_url, paths, budget=budget, product_contracts=loaded)
                )
            except VerifyFailure as exc:
                results.append(
                    _failed_area(
                        "api", exc.detail, check=exc.check, base_url=base_url, budget_s=budget
                    )
                )
        elif area == "sources":
            try:
                results.append(run_sources(sources_url, word, budget=budget))
            except VerifyFailure as exc:
                results.append(
                    _failed_area("sources", exc.detail, base_url=sources_url)
                )
        else:
            try:
                results.append(run_host())
            except VerifyFailure as exc:
                results.append(_failed_area("host", exc.detail))
    return build_receipt(results)


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog=f"{_MACHINERY}.ops.verify_run",
        description=(
            "Check that Monitor pages and GET routes, the Sources server, or the pinned host units answer.\n"
            "Run it before commit and before enqueue when a change touches that area. "
            "Do not use it as a load test, and do not point it at anything but loopback."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            f"  .venv/bin/python -m {_MACHINERY}.ops.verify_run --area api\n"
            f"  .venv/bin/python -m {_MACHINERY}.ops.verify_run --area sources\n"
            f"  .venv/bin/python -m {_MACHINERY}.ops.verify_run --area host --area sources\n"
            "\n"
            "Outputs:\n"
            "  A JSON receipt on stdout (schema_version verify-run.v1). "
            "--receipt also writes that same document.\n"
            "  The receipt has status, elapsed time, and a short detail. It has no response bodies.\n"
            "\n"
            "Exit codes:\n"
            "  0  every requested check passed.\n"
            "  1  a check failed, timed out, or could not be set up.\n"
            "  2  the arguments are wrong.\n"
            "\n"
            "Related:\n"
            "  agents_extensions/shared/skills/verify/SKILL.md maps each area to this command.\n"
            "  Page and route contracts: lu_machinery/api/route_contracts.py "
            "(all_page_contracts, all_route_contracts).\n"
            "  Pinned units: Requires= in packaging/systemd/learn-ukrainian-loopback.target.\n"
            "  Sources: the learn-ukrainian-sources unit's loopback /health and /mcp verify_word."
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    parser.add_argument(
        "--area",
        action="append",
        required=True,
        choices=AREAS,
        metavar="AREA",
        help="Area to check: api, sources, or host. Repeat the flag to check more than one. Example: --area api",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_MONITOR,
        help=f"Loopback Monitor origin for --area api (default: {DEFAULT_MONITOR}). Example: http://127.0.0.1:8765",
    )
    parser.add_argument(
        "--sources-url",
        default=DEFAULT_SOURCES,
        help=f"Loopback Sources origin for --area sources (default: {DEFAULT_SOURCES}). Example: http://127.0.0.1:8766",
    )
    parser.add_argument(
        "--word",
        default=DEFAULT_WORD,
        help=f"Ukrainian form passed to one verify_word call (default: {DEFAULT_WORD}). Example: мова",
    )
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=DEFAULT_BUDGET_S,
        help=(
            "Seconds each API GET and the Sources /health check must finish within "
            f"(default: {DEFAULT_BUDGET_S:g}). The verify_word call allows {TOOL_TIMEOUT_S:g}s."
        ),
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        default=None,
        help="Also write the JSON receipt to this path. Stdout always receives it. Default: stdout only.",
    )
    args = parser.parse_args(argv)
    if not args.word.strip() or args.budget_seconds <= 0:
        parser.error("--word must be non-empty and --budget-seconds must be positive")
    try:
        receipt = run(
            args.area,
            base_url=args.base_url,
            sources_url=args.sources_url,
            word=args.word,
            budget=args.budget_seconds,
        )
    except VerifyFailure as exc:
        print(f"REFUSED: {exc.detail}", file=sys.stderr)
        return 1
    text = json.dumps(receipt, indent=2, ensure_ascii=False) + "\n"
    sys.stdout.write(text)
    if args.receipt is not None:
        args.receipt.write_text(text, encoding="utf-8")
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
