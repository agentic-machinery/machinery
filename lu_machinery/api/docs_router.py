"""Documentation artifacts API router (#1814).

Mounted at /artifacts and /files in main.py.

Serves static documentation (HTML, MD, assets) and directory listings
from approved roots.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

try:
    from lu_machinery.path_safety import safe_join  # scripts/ on sys.path
except ImportError:
    from ..path_safety import safe_join  # scripts.api package import

from .monitor_context import MonitorContext, get_ctx, resolve_context
from .product_contribution import product_file

logger = logging.getLogger(__name__)

router = APIRouter(tags=["docs"])

_FRONTMATTER_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_HTML_META_RE = re.compile(
    r'<meta\s+name="report-([^"]+)"\s+content="([^"]*)"\s*/?>',
    re.IGNORECASE,
)
_HEAD_BYTES = 8192

# Directory listing page size: the default, and the most one request may ask for.
LISTING_PAGE_SIZE = 100
LISTING_PAGE_MAX = 500

# Path prefixes (relative to project_root) to exclude from discovery.
DISCOVERY_EXCLUDES = ("docs/archive", "docs/resources/podcasts/raw")


def build_allowed_roots(project_root: Path) -> dict[str, Path]:
    """Return approved documentation root directories under project_root."""
    resolved_root = Path(project_root)
    return {
        "audit": resolved_root / "audit",
        "session-state": resolved_root / "docs" / "session-state",
        "handoffs": resolved_root / "docs" / "handoffs",
        "reports": resolved_root / "docs" / "reports",
        "architecture": resolved_root / "docs" / "architecture",
        "best-practices": resolved_root / "docs" / "best-practices",
        "decisions": resolved_root / "docs" / "decisions",
        "references": resolved_root / "docs" / "references" / "external",
        "proposals": resolved_root / "docs" / "proposals",
        "poc": resolved_root / "docs" / "poc",
    }


def build_effective_roots(project_root: Path) -> dict[str, Path]:
    """Build effective roots as allowed roots union discovered docs/ dirs."""
    resolved_root = Path(project_root)
    effective = build_allowed_roots(resolved_root)
    docs_dir = resolved_root / "docs"
    if docs_dir.is_dir():
        for entry in sorted(docs_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            root_key = f"docs/{entry.name}"
            # Skip if this path prefix is excluded
            if any(root_key.startswith(excl) for excl in DISCOVERY_EXCLUDES):
                continue
            effective.setdefault(root_key, entry)
    return effective


_MIME_TYPES = {
    ".html": "text/html",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".css": "text/css",
    # A script subresource executes in the embedding page's origin. Serve
    # stored scripts as text so nosniff blocks that use.
    ".js": "text/plain",
    ".gif": "image/gif",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def _iso_from_timestamp(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat().replace("+00:00", "Z")


def _split_csv_numbers(value: str | None) -> list[int]:
    items: list[int] = []
    for raw in (value or "").split(","):
        raw = raw.strip().lstrip("#")
        if raw.isdigit():
            items.append(int(raw))
    return items


def _split_csv_strings(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _read_head(path: str | Path) -> str | None:
    """Return the first 8KB of text, or None if the file cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(_HEAD_BYTES)
    except Exception:
        return None


def _parse_html_meta(head: str) -> dict[str, str]:
    """Extract report-* meta tags from an HTML head already in memory."""
    return {match.group(1): match.group(2) for match in _HTML_META_RE.finditer(head)}


def _h1_title(text: str) -> str | None:
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return stripped[2:].strip()
    return None


def _parse_md_frontmatter(head: str, path: str | Path) -> dict[str, str]:
    """Parse a markdown head already in memory.

    Same rules as the file reader: a leading ``---`` block, then the first H1
    when the block has no title. Malformed YAML is skipped.
    """
    meta: dict[str, str] = {}
    if not head.startswith("---"):
        title = _h1_title(head)
        if title is not None:
            meta["title"] = title
        return meta

    end_idx = head.find("\n---", 3)
    if end_idx == -1:
        return meta

    try:
        data = yaml.load(head[4:end_idx], Loader=_FRONTMATTER_LOADER)
        if isinstance(data, dict):
            for key, value in data.items():
                if value is not None:
                    meta[str(key)] = str(value)
    except Exception:
        logger.debug("Malformed YAML frontmatter in %s, skipping metadata", path)

    if "title" not in meta:
        title = _h1_title(head[end_idx + 4:])
        if title is not None:
            meta["title"] = title
    return meta


def _extract_html_meta(path: Path) -> dict[str, str]:
    """Extract <meta name="report-*" content="..."> tags from HTML."""
    if path.suffix.lower() != ".html" or not path.is_file():
        return {}
    head = _read_head(path)
    if head is None:
        return {}
    return _parse_html_meta(head)


def _extract_md_frontmatter(path: Path) -> dict[str, str]:
    """Extract YAML frontmatter from MD files.

    Parses the leading YAML block delimited by ``---`` / ``---``.
    Falls back to first H1 as title if no frontmatter ``title`` key.
    Limits reads to 8KB head. Returns empty dict on any error.
    """
    if path.suffix.lower() != ".md" or not path.is_file():
        return {}
    head = _read_head(path)
    if head is None:
        return {}
    return _parse_md_frontmatter(head, path)


def _project_prefix(project_root: Path) -> str:
    text = os.fspath(project_root)
    return text if text.endswith(os.sep) else text + os.sep


def _discovery_ancestry_verified(discovery: str) -> bool:
    """True when a non-following walk from ``discovery`` cannot leave that directory.

    ``.`` and ``..`` components, and a symlink anywhere in the path, make the
    lexical prefix a different place from ``Path.resolve()``. Those walks keep
    the resolved containment check. One realpath of the start directory covers
    the whole walk: directory symlinks below it are not followed.
    """
    if any(part in {".", ".."} for part in discovery.split(os.sep)):
        return False
    try:
        return os.path.realpath(discovery) == discovery
    except OSError:
        return False


def _is_excluded(rel_posix: str) -> bool:
    return any(rel_posix == excl or rel_posix.startswith(f"{excl}/") for excl in DISCOVERY_EXCLUDES)


def _relative_under(path: str, root: str) -> str | None:
    """Return the posix path of ``path`` under ``root``, or None if it is not a lexical child.

    This is the string form of ``_relative_to_root`` for a walk that never
    follows directory links. It does not resolve symlinks.
    """
    if path == root:
        return "."
    prefix = root if root.endswith(os.sep) else root + os.sep
    if not path.startswith(prefix):
        return None
    return path[len(prefix):].replace(os.sep, "/")


def _iter_discovery_files(project_prefix: str) -> Iterator[tuple[str, str, str, bool, bool]]:
    """Yield ``(path, name, project-relative posix, is_symlink, ancestry_verified)``.

    Directory symlinks are not followed, matching ``Path.rglob``. Hidden names
    and ``DISCOVERY_EXCLUDES`` are pruned. Unreadable directories are skipped.
    ``ancestry_verified`` is false when the discovery path itself contains
    ``.`` or ``..`` or any symlink, so the caller still resolves those files.
    """
    for name in ("docs", "audit"):
        discovery = project_prefix + name
        if not os.path.isdir(discovery):
            continue
        verified = _discovery_ancestry_verified(discovery)
        stack = [discovery]
        while stack:
            current = stack.pop()
            children: list[tuple[str, str, str, bool, bool]] = []
            subdirs: list[str] = []
            try:
                with os.scandir(current) as scanned:
                    for entry in scanned:
                        if entry.name.startswith("."):
                            continue
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        if is_dir:
                            if not entry.path.startswith(project_prefix):
                                continue
                            rel_dir = entry.path[len(project_prefix):].replace(os.sep, "/")
                            if _is_excluded(rel_dir):
                                continue
                            subdirs.append(entry.path)
                            continue
                        try:
                            is_file = entry.is_file(follow_symlinks=True)
                        except OSError:
                            continue
                        if not is_file or not entry.path.startswith(project_prefix):
                            continue
                        rel = entry.path[len(project_prefix):].replace(os.sep, "/")
                        if _is_excluded(rel):
                            continue
                        try:
                            symlink = entry.is_symlink()
                        except OSError:
                            symlink = True
                        children.append((entry.path, entry.name, rel, symlink, verified))
            except OSError:
                continue
            yield from children
            stack.extend(subdirs)


def _get_root_info(path: str, effective_roots: Mapping[str, Path]) -> tuple[str, Path, str] | None:
    """Find the matching root for a given path.

    Returns (root_key, root_path, relative_remainder) or None.
    """
    # Sort by length descending to match most specific root first
    for root_key in sorted(effective_roots.keys(), key=len, reverse=True):
        if path == root_key or path.startswith(f"{root_key}/"):
            remainder = path[len(root_key):].lstrip("/")
            return root_key, effective_roots[root_key], remainder
    return None


def _is_hidden_path(remainder: str) -> bool:
    return any(part.startswith(".") for part in Path(remainder).parts)


def _assert_under_root(full_path: Path, root_path: Path) -> None:
    # codeql[py/path-injection] -- this is the resolved path-traversal check (#1860).
    try:
        resolved_path = full_path.resolve()
        # A canonical root already appears in the resolved path. Resolve the
        # root separately only when it may be a symlink alias.
        if not resolved_path.is_relative_to(root_path):
            _relative_to_root(resolved_path, root_path.resolve())
    except ValueError as e:
        raise HTTPException(status_code=403, detail="Path traversal not allowed") from e


def _undiscoverable(full_path: Path, root_path: Path, project_root: Path) -> bool:
    """True when discovery would never list ``full_path``.

    Discovery prunes ``DISCOVERY_EXCLUDES`` and hidden names. Direct reads and
    listings apply the same rule to the requested path and to its resolved
    target, so an in-root symlink cannot reach an excluded subtree or a dotfile.
    """
    lexical = (full_path, root_path, project_root)
    resolved = (full_path.resolve(), root_path.resolve(), project_root.resolve())
    for path, root, project in (lexical, resolved):
        try:
            below_root = _relative_to_root(path, root)
        except ValueError:
            below_root = "."
        if any(part.startswith(".") for part in below_root.split("/") if part != "."):
            return True
        try:
            if _is_excluded(_relative_to_root(path, project)):
                return True
        except ValueError:
            continue
    return False


def _relative_to_root(path: Path, root: Path) -> str:
    """Return a stable root-relative path across symlinked checkout roots."""
    # Compare complete lexical components, without constructing every ancestor
    # of root (Path.relative_to does that on Python 3.12, even without walk_up).
    # Resolved containment remains the responsibility of _assert_under_root.
    root_parts = root.parts
    path_parts = path.parts
    if path.anchor != root.anchor or path_parts[:len(root_parts)] != root_parts:
        raise ValueError(f"{str(path)!r} is not in the subpath of {str(root)!r}")
    return "/".join(path_parts[len(root_parts):]) or "."


def _directory_listing(
    path: str, root_key: str, root_path: Path, remainder: str, *, limit: int, offset: int, project_root: Path,
) -> dict:
    """One page of a directory, in name order, with the count of every listed entry."""
    full_path = safe_join(root_path, remainder) if remainder else safe_join(root_path)
    _assert_under_root(full_path, root_path)
    entries = [entry for entry in sorted(full_path.iterdir()) if not entry.name.startswith(".")]
    # Every entry is checked, so one that escapes the root refuses all pages alike.
    for entry in entries:
        _assert_under_root(entry, root_path)
    entries = [entry for entry in entries if not _undiscoverable(entry, root_path, project_root)]
    items = []
    for entry in entries[offset:offset + limit]:
        stat = entry.stat()
        item = {
            "name": entry.name,
            "is_dir": entry.is_dir(),
            "size": stat.st_size if not entry.is_dir() else None,
            "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "path": f"{path}/{entry.name}".replace("//", "/"),
        }
        if not entry.is_dir() and entry.suffix.lower() == ".html":
            item["meta"] = _extract_html_meta(entry)
        items.append(item)
    return {
        "root": root_key,
        "relative_path": remainder,
        "items_count": len(items),
        "total": len(entries),
        "limit": limit,
        "offset": offset,
        "items": items,
    }


def _page_url(*, format: str | None, limit: int, offset: int) -> str:
    """Query-only relative link to another page of the current listing.

    Built solely from the recognised format and paging parameters so arbitrary query
    parameters are never echoed and directory segments cannot trigger path redaction.
    """
    params: dict[str, str | int] = {"limit": limit, "offset": offset}
    if format is not None:
        params = {"format": format, **params}
    return f"?{urlencode(params)}"


def collect_artifacts(
    *,
    class_filter: str | None = None,
    date_from: str | None = None,
    status: str | None = None,
    author: str | None = None,
    types: tuple[str, ...] = ("html", "md"),
    limit: int | None = None,
    offset: int = 0,
    ctx: MonitorContext | None = None,
) -> dict:
    """Return all HTML and MD artifacts under discovery roots.

    Walks docs/ and audit/ once, without following directory symlinks. Skips
    DISCOVERY_EXCLUDES and dotfiles. Matches each file to an effective_roots
    entry for serving. Parses HTML <meta> tags and MD YAML frontmatter.

    Args:
        class_filter: Filter by artifact class.
        date_from: ISO date lower bound.
        status: Filter by status.
        author: Filter by author.
        types: Tuples of extensions to include (default: html, md).
        limit: Page size. ``None`` returns every match from ``offset`` on.
        offset: Matches to skip, newest first. ``total`` always counts every match.
        ctx: Application context providing filesystem roots.
    """
    if (limit is not None and limit < 1) or offset < 0:
        raise ValueError("limit must be at least 1 and offset must not be negative")
    resolved_ctx = resolve_context(ctx)
    project_root = resolved_ctx.roots.project_root
    project_prefix = _project_prefix(project_root)
    # Longest key wins. Sorted once per collect, not once per file.
    root_rows = tuple(
        sorted(
            ((key, os.fspath(path), path) for key, path in resolved_ctx.roots.effective_roots.items()),
            key=lambda item: len(item[0]),
            reverse=True,
        )
    )

    artifacts: list[dict] = []
    type_set = set(types)

    for path, name, rel, symlink, ancestry_verified in _iter_discovery_files(project_prefix):
        suffix = os.path.splitext(name)[1].lower()
        if suffix == ".html":
            if "html" not in type_set:
                continue
            kind = "html"
        elif suffix == ".md":
            if "md" not in type_set:
                continue
            kind = "md"
        else:
            continue

        root_key: str | None = None
        root_text = ""
        root_path: Path | None = None
        for key, text, candidate in root_rows:
            if rel == key or rel.startswith(f"{key}/"):
                root_key = key
                root_text = text
                root_path = candidate
                break
        if root_key is None or root_path is None:
            continue

        # Path.resolve() realpaths every parent and dominated a cold collect.
        # Skip it only when this walk's start directory is a real lexical path
        # (no ".", "..", or symlink) and the file itself is not a symlink.
        # Anything else still goes through _assert_under_root, which rejects
        # a resolved path outside the serving root with HTTP 403.
        url_rel = _relative_under(path, root_text)
        if symlink or not ancestry_verified or url_rel is None or ".." in path.split(os.sep):
            _assert_under_root(Path(path), root_path)
        if url_rel is None:
            url_rel = _relative_to_root(Path(path), root_path)

        st = os.stat(path)
        head = _read_head(path)
        if head is None:
            meta: dict[str, str] = {}
        elif kind == "html":
            meta = _parse_html_meta(head)
        else:
            meta = _parse_md_frontmatter(head, path)

        report_class = meta.get("class") or "document"
        report_status = meta.get("status") or "unknown"
        modified_at = _iso_from_timestamp(st.st_mtime)
        report_date = meta.get("date") or modified_at[:10]
        report_author = meta.get("author") or ""

        if class_filter and report_class != class_filter:
            continue
        if status and report_status != status:
            continue
        if author and report_author != author:
            continue
        if date_from and report_date < date_from:
            continue

        artifacts.append(
            {
                "path": rel,
                "url": f"/artifacts/{root_key}/{url_rel}",
                "class": report_class,
                "date": report_date,
                "status": report_status,
                "title": meta.get("title") or os.path.splitext(name)[0],
                "kpi_summary": meta.get("kpi_summary") or meta.get("kpi-summary") or "",
                "related_issues": _split_csv_numbers(meta.get("related_issues") or meta.get("related-issues")),
                "related_prs": _split_csv_numbers(meta.get("related_prs") or meta.get("related-prs")),
                "agents": _split_csv_strings(meta.get("agents")),
                "author": report_author,
                "size_bytes": st.st_size,
                "modified_at": modified_at,
            }
        )

    artifacts.sort(key=lambda item: (item["date"], item["modified_at"], item["path"]), reverse=True)
    return {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "total": len(artifacts),
        "artifacts": artifacts[offset:None if limit is None else offset + limit],
    }


# Backward-compat alias — artifacts_router.py originally imported this name.
collect_html_artifacts = collect_artifacts


def _artifacts_page(request: Request, ctx: MonitorContext) -> FileResponse:
    """The artifact browser page: a machinery dashboard file, else the page the product declares.

    The wheel ships no artifact page of its own, so a product that declares none gets a 404.
    """
    machinery_page = ctx.roots.dashboards_dir / "artifacts.html"
    if machinery_page.is_file():
        return FileResponse(machinery_page, media_type="text/html")
    product_page = product_file(getattr(request.app.state, "product", None), "artifacts.html")
    if product_page is None:
        raise HTTPException(status_code=404, detail="No artifact browser page is installed")
    return FileResponse(product_page, media_type="text/html")


@router.get("/")
def list_roots(
    request: Request,
    format: str | None = Query(None, pattern="^(json)$"),
    ctx: MonitorContext = Depends(get_ctx),
):
    """List all approved documentation roots."""
    resolved_ctx = resolve_context(ctx)
    if request.url.path.startswith("/artifacts") and format != "json":
        return _artifacts_page(request, resolved_ctx)
    return {
        "roots": [
            {
                "id": k,
                "path": _relative_to_root(v, resolved_ctx.roots.project_root),
                "exists": v.exists(),
            }
            for k, v in resolved_ctx.roots.effective_roots.items()
        ]
    }


@router.get("/{path:path}")
def serve_artifact(
    path: str,
    request: Request,
    format: str | None = Query(None, pattern="^(json)$"),
    limit: int = Query(LISTING_PAGE_SIZE, ge=1, le=LISTING_PAGE_MAX),
    offset: int = Query(0, ge=0),
    ctx: MonitorContext = Depends(get_ctx),
):
    """Serve a documentation artifact or list one page of a directory."""
    resolved_ctx = resolve_context(ctx)
    if not path:
        return list_roots(request, format, ctx=resolved_ctx)

    info = _get_root_info(path, resolved_ctx.roots.effective_roots)
    if not info:
        raise HTTPException(status_code=403, detail="Path not under an approved documentation root")

    root_key, root_path, remainder = info
    if _is_hidden_path(remainder):
        raise HTTPException(status_code=403, detail="Hidden files are not served as documentation artifacts")
    try:
        full_path = safe_join(root_path, remainder) if remainder else safe_join(root_path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    _assert_under_root(full_path, root_path)
    # Checked before existence, so the answer never says whether an excluded path exists.
    if _undiscoverable(full_path, root_path, resolved_ctx.roots.project_root):
        raise HTTPException(status_code=403, detail="Path is excluded from documentation artifacts")

    # codeql[py/path-injection] -- full_path validated via safe_join + _assert_under_root above. See #1860.
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")

    # codeql[py/path-injection] -- full_path validated via safe_join + _assert_under_root above. See #1860.
    if full_path.is_dir():
        if request.url.path.startswith("/artifacts") and format != "json":
            return _artifacts_page(request, resolved_ctx)
        # One fixed grid of pages: "previous" and "next" then never skip, repeat or resize a page.
        if offset % limit:
            raise HTTPException(status_code=422, detail="offset must be a multiple of limit")
        listing = _directory_listing(
            path, root_key, root_path, remainder, limit=limit, offset=offset,
            project_root=resolved_ctx.roots.project_root,
        )
        last = max(0, listing["total"] - 1) // limit * limit
        # Past the end, "previous" is the last page that has rows.
        previous = min(offset - limit, last) if offset else None
        following = offset + limit if offset + limit < listing["total"] else None
        return {
            **listing,
            "previous": None if previous is None else _page_url(format=format, limit=limit, offset=previous),
            "next": None if following is None else _page_url(format=format, limit=limit, offset=following),
        }

    # File serving
    media_type = _MIME_TYPES.get(full_path.suffix.lower())
    if media_type is None:
        raise HTTPException(
            status_code=403,
            detail=f"File extension {full_path.suffix} not allowed for documentation artifacts",
        )

    # codeql[py/path-injection] -- full_path validated via safe_join + _assert_under_root + extension allowlist above. See #1860.
    return FileResponse(
        full_path,
        media_type=media_type,
        headers={
            "Cache-Control": "max-age=300, must-revalidate",
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )
