"""Process-wide TTL cache and single-flight helpers shared by Monitor routers.

Keys are strings chosen by each caller and scoped per app context with
:func:`ctx_cache_scope`. The curriculum readers that used to live here are
product code in ``scripts.project_api.curriculum_state`` (Monitor split,
C-4687).
"""

from __future__ import annotations

# ==================== TTL CACHE ====================
#
# In-process cache shared across routers. Keys are strings chosen by
# each caller (conventionally prefixed with the router/feature name,
# e.g. "orient_git", "summary"). Values are whatever the caller wants
# to cache. TTL is measured in seconds.
#
# Timestamps use ``time.monotonic()`` so TTL is resilient to wall-clock
# jumps (NTP sync, DST, manual clock edits) — see GH #1309 reviewer
# BLOCKER. Monotonic values are only valid within this process; the
# dict is in-memory anyway, so restart clears it naturally.
#
# The clock is a module-level seam so tests can advance cache time
# explicitly and wait on completion signals instead of asserting
# wall-clock staleness budgets on loaded CI runners (#8583).
import asyncio
import concurrent.futures
import functools
import inspect
import logging
import threading
import time
import weakref
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import HTTPException, Response

from .panel_values import snapshot_headers

if TYPE_CHECKING:
    from .monitor_context import MonitorContext

_ttl_cache: dict[str, tuple[float, object]] = {}
_ttl_cache_lock = threading.Lock()
# prefix -> the only key still allowed to publish under that prefix.
_retained: dict[str, str] = {}
_inflight_futures: dict[str, concurrent.futures.Future] = {}
_inflight_lock = threading.Lock()
_ttl_clock: Callable[[], float] = time.monotonic

T = TypeVar("T")

# A background refresher runs only while its result is being read. Two minutes
# spans four 30 s page polls, so an open page never lets its panels go dormant.
DEMAND_WINDOW_S = 120.0
# A read that finds a snapshot writer dormant waits this long, off the event
# loop, for one fresh collection. Callers that act on the list (worktree
# cleanup, hand-off readiness) never get a minutes-old observation that way.
# It matches the Retry-After of a snapshot that has no observation yet.
DORMANT_READ_WAIT_S = 2.0
# A stale-while-revalidate route read that finds its value dormant waits this
# long for the refresh it starts; under the 1 s panel budget, then the stored
# value with its true age. A first read, with nothing stored yet, waits the same
# bound for the first computation, then answers the typed warming 503.
SWR_DORMANT_READ_WAIT_S = 0.5
# Retry-After of every warming 503: the first collection usually lands by then.
WARMING_RETRY_AFTER_S = 2


def warming_unavailable(detail: str) -> HTTPException:
    """The typed 503 a cold read gets when its first collection is still running."""
    return HTTPException(
        status_code=503, detail=detail,
        headers={
            "X-Snapshot-Age-S": "unknown", "Retry-After": str(WARMING_RETRY_AFTER_S), "Cache-Control": "no-store",
        },
    )


class Demand:
    """When a refresher's result was last read. Without recent reads it does no work."""

    def __init__(self) -> None:
        self._last_read = float("-inf")

    def touch(self) -> bool:
        """Record a read. True when the refresher had gone dormant before this read."""
        now = _ttl_clock()
        dormant = now - self._last_read >= DEMAND_WINDOW_S
        self._last_read = now
        return dormant

    def active(self) -> bool:
        return _ttl_clock() - self._last_read < DEMAND_WINDOW_S


# Snapshot writers whose lifespan is running; startup work waits for their first collections.
_running_snapshots: weakref.WeakSet[PeriodicSnapshot[Any]] = weakref.WeakSet()


async def first_collections_settled(timeout: float) -> None:
    """Wait, at most ``timeout`` seconds, until every snapshot writer on this loop has settled its first collection.

    Startup runs this before the heavy warmups, so the panels' first
    collections get the task-scan lock and the GIL first. A collection that
    takes longer than the bound stops holding the warmups back; it is not
    cancelled.
    """
    loop = asyncio.get_running_loop()
    events = [snapshot._ready for snapshot in list(_running_snapshots)
              if snapshot._loop is loop and snapshot._ready is not None]
    if events:
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in events)), timeout=timeout)


class PeriodicSnapshot[T]:
    """One lifespan-owned writer; readers never collect.

    The writer refreshes every ``interval`` only while the snapshot is read
    (:data:`DEMAND_WINDOW_S`). Unread, it sleeps until the next read. That read
    wakes it and, off the event loop, waits up to ``dormant_wait_s`` (default
    :data:`DORMANT_READ_WAIT_S`) for the fresh collection; otherwise it gets
    the last observation with its true age. Reads while the writer is active
    never wait, except before the first collection settles: a read off the
    event loop then joins that collection for the same bounded wait, and gets
    the typed warming 503 only if it is still running.
    """

    def __init__(self, compute: Callable[[], T], interval: float, *, dormant_wait_s: float | None = None):
        self.compute = compute
        self.interval = interval
        self.dormant_wait_s = dormant_wait_s
        self._entry: tuple[float, T] | None = None
        self._demand = Demand()
        self._demand.touch()  # the startup collection
        self._wake: asyncio.Event | None = None
        self._ready: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._attempts = 0
        # Attempt count when a read woke the dormant writer; None once it settles.
        self._pending: int | None = None
        self._settled = threading.Condition()

    def refresh(self) -> None:
        # Age includes collection time, rather than making a slow scan look new.
        started = _ttl_clock()
        value = self.compute()
        self._entry = (started, value)

    def read(self) -> tuple[T, float]:
        self._join_wake_refresh()
        entry = self._entry
        if entry is None:
            raise warming_unavailable("snapshot not ready")
        return entry[1], max(0.0, _ttl_clock() - entry[0])

    def _join_wake_refresh(self) -> None:
        """Wake a dormant writer; off its event loop, wait for that refresh to settle.

        Every reader that arrives before the wake-up refresh settles joins it,
        not only the one that woke the writer.
        """
        loop, wake = self._loop, self._wake
        if loop is None or wake is None:
            self._demand.touch()
            return
        with self._settled:
            woke = self._demand.touch() and self._pending is None
            if woke:
                self._pending = self._attempts
            pending = self._pending
            if pending is None and self._entry is None and self._attempts == 0:
                pending = 0  # the first collection has not settled: join it
        if woke:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:  # the loop already closed at shutdown
                with self._settled:
                    self._pending = None
                return
        if pending is None:
            return
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            return  # the writer needs this thread; answer from the last observation
        with self._settled:
            wait_s = DORMANT_READ_WAIT_S if self.dormant_wait_s is None else self.dormant_wait_s
            self._settled.wait_for(lambda: self._attempts != pending, timeout=wait_s)

    async def _refresh(self) -> None:
        work = asyncio.create_task(asyncio.to_thread(self.refresh))
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            # Join the thread without letting its failure replace cancellation.
            with suppress(Exception):
                await work
            raise

    @asynccontextmanager
    async def lifespan(self):
        ready = asyncio.Event()
        wake = asyncio.Event()
        self._loop, self._wake, self._ready = asyncio.get_running_loop(), wake, ready
        _running_snapshots.add(self)

        async def repeat() -> None:
            while True:
                wake.clear()
                if not self._demand.active():
                    await wake.wait()
                    continue
                try:
                    await self._refresh()
                except Exception:
                    _cache_log.warning("snapshot refresh failed", exc_info=True)
                finally:
                    ready.set()
                    with self._settled:
                        self._attempts += 1
                        self._pending = None
                        self._settled.notify_all()
                with suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=self.interval)

        task = asyncio.create_task(repeat(), name="monitor-snapshot-refresh")
        try:
            # Usually fixtures/small stores are ready immediately. A cold task
            # scan must not hold ASGI startup (and every unrelated panel) hostage.
            # Keep the same single writer running; read() supplies the existing
            # typed 503 + Retry-After until its first observation is available.
            with suppress(TimeoutError):
                await asyncio.wait_for(ready.wait(), timeout=0.1)
            yield
        finally:
            _running_snapshots.discard(self)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self._ready = None
            with self._settled:  # no reader waits on a writer that has stopped
                self._loop = self._wake = self._pending = None
                self._settled.notify_all()


def ctx_cache_scope(ctx: MonitorContext | None) -> str:
    """Cache-key / single-slot scope for one app context (#7494).

    Two app instances with different roots must never share TTL entries or
    thrash each other's last-good slots. Empty string when ``ctx`` is absent
    (plain-Python callers that have not resolved a context yet).
    """
    if ctx is None:
        return ""
    return f"@{ctx.roots.project_root}"


def ctx_scoped_ttl_key(ctx: MonitorContext, *parts: object) -> str:
    """Canonical ctx-scoped TTL-cache key: ``<resolved project root>:<parts...>`` (#7494).

    Single source of truth for the key format so a writer in one router
    (e.g. the state router's summary endpoint) and a reader in another
    (e.g. the dashboard overview) always address the same entry within one
    app context, while two app instances with different roots stay isolated.
    """
    root = str(ctx.roots.project_root.resolve())
    return ":".join((root, *(str(p) for p in parts)))


def cache_get(key: str, ttl: float) -> object | None:
    """Return cached value if still within TTL, else None."""
    now = _ttl_clock()
    with _ttl_cache_lock:
        entry = _ttl_cache.get(key)
    if entry and (now - entry[0]) < ttl:
        return entry[1]
    return None


def cache_get_with_age(key: str, ttl: float) -> tuple[object, float] | None:
    """Return cached value and age if still within TTL, else None."""
    now = _ttl_clock()
    with _ttl_cache_lock:
        entry = _ttl_cache.get(key)
    if entry is None:
        return None
    age = now - entry[0]
    if age < ttl:
        return entry[1], age
    return None


def _generation_superseded(key: str) -> bool:
    """True when a newer generation has replaced this key under its prefix."""
    return any(key.startswith(prefix) and key != current for prefix, current in _retained.items())


def cache_set(key: str, value: object) -> None:
    """Store a value in the TTL cache.

    A generation that ``cache_retain`` already replaced does not publish again.
    """
    now = _ttl_clock()
    with _ttl_cache_lock:
        if _generation_superseded(key):
            return
        _ttl_cache[key] = (now, value)


def cache_get_or_compute(  # noqa: UP047 — ruff pyflakes lacks PEP 695 type-param support here
    key: str,
    ttl: float,
    compute: Callable[[], T],
    *,
    force: bool = False,
) -> T:
    """Return a TTL-cached value, coalescing concurrent misses into one compute.

    Used for expensive curriculum walks (``weak-points``, ``pipeline-versions``)
    so a stampede of cold GETs cannot multiply a ~2k-module scan (#7973).
    ``force=True`` bypasses a warm entry (``?fresh=true``) but still coalesces
    concurrent forced recomputes onto a single flight for the same key.
    """
    if not force:
        cached = cache_get(key, ttl)
        if cached is not None:
            return cached  # type: ignore[return-value]

    fut, leader, cached = _claim_flight(key, ttl, force=force)
    if cached is not None:
        return cached  # type: ignore[return-value]
    assert fut is not None
    if not leader:
        return fut.result(timeout=180)
    return _finish_flight(key, fut, compute)


def cache_get_stored(key: str) -> tuple[object, float] | None:
    """Return a stored value and how long it has been stored, ignoring TTL."""
    now = _ttl_clock()
    with _ttl_cache_lock:
        entry = _ttl_cache.get(key)
    if entry is None:
        return None
    return entry[1], now - entry[0]


def cache_retain(prefix: str, key: str) -> int:
    """Drop every other entry under ``prefix`` so a new generation replaces the old one.

    Later ``cache_set`` calls for a replaced key are ignored, including a compute
    that was already in flight when the generation changed.
    """
    if not prefix:
        return 0
    with _ttl_cache_lock:
        _retained[prefix] = key
        stale = [item for item in _ttl_cache if item.startswith(prefix) and item != key]
        for item in stale:
            _ttl_cache.pop(item, None)
        return len(stale)


_lead_tasks: set[asyncio.Task] = set()
_refresh_keys: set[str] = set()
_refresh_keys_lock = threading.Lock()
_cache_log = logging.getLogger("state_helpers")


def _claim_flight(key: str, ttl: float, *, force: bool) -> tuple[concurrent.futures.Future | None, bool, object | None]:
    """Claim the single in-flight compute shared by sync and async callers.

    Returns ``(future, is_leader, cached)``. A cached hit has no future.
    A finished flight is not reused, so a later caller can start the next one.
    """
    with _inflight_lock:
        if not force:
            cached = cache_get(key, ttl)
            if cached is not None:
                return None, False, cached
        fut = _inflight_futures.get(key)
        if fut is None or fut.done():
            fut = concurrent.futures.Future()
            _inflight_futures[key] = fut
            return fut, True, None
        return fut, False, None


def _finish_flight(  # noqa: UP047 — ruff pyflakes lacks PEP 695 type-param support here
    key: str,
    fut: concurrent.futures.Future,
    compute: Callable[[], T],
) -> T:
    """Run compute on the caller's thread and publish one result for every waiter."""
    try:
        value = compute()
        cache_set(key, value)
        if not fut.done():
            fut.set_result(value)
        return value
    except BaseException as exc:
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        with _inflight_lock:
            if _inflight_futures.get(key) is fut:
                _inflight_futures.pop(key, None)


async def cache_get_or_compute_async(  # noqa: UP047 — ruff pyflakes lacks PEP 695 type-param support here
    key: str,
    ttl: float,
    compute: Callable[[], T],
    *,
    force: bool = False,
) -> T:
    """TTL read that stays on the event loop when the value is already warm.

    A miss starts one worker thread. Sync warmup and other requests for the
    same key wait on that same flight, so an older compute cannot overwrite a
    newer one. Cancelling one waiter does not cancel the shared result.
    """
    if not force:
        cached = cache_get(key, ttl)
        if cached is not None:
            return cached  # type: ignore[return-value]

    fut, leader, cached = _claim_flight(key, ttl, force=force)
    if cached is not None:
        return cached  # type: ignore[return-value]
    assert fut is not None

    if leader:

        async def _lead() -> None:
            try:
                value = await asyncio.to_thread(compute)
                cache_set(key, value)
                if not fut.done():
                    fut.set_result(value)
            except Exception as exc:
                if not fut.done():
                    fut.set_exception(exc)
            finally:
                with _inflight_lock:
                    if _inflight_futures.get(key) is fut:
                        _inflight_futures.pop(key, None)

        task = asyncio.create_task(_lead())
        _lead_tasks.add(task)
        task.add_done_callback(_lead_tasks.discard)

    # shield: cancelling this waiter must not cancel the shared future.
    return await asyncio.shield(asyncio.wrap_future(fut))


def schedule_cache_refresh(
    key: str, ttl: float, compute: Callable[[], object], *, should_refresh: Callable[[], bool] | None = None,
) -> None:
    """Check and recompute off-loop, sharing one flight without renewing unchanged data."""

    def _run() -> None:
        try:
            if should_refresh is None or should_refresh():
                cache_get_or_compute(key, ttl, compute, force=True)
        except Exception:
            _cache_log.debug("background cache refresh failed", exc_info=True)
        finally:
            with _refresh_keys_lock:
                _refresh_keys.discard(key)

    with _refresh_keys_lock:
        if key in _refresh_keys:
            return
        _refresh_keys.add(key)
    threading.Thread(target=_run, name="status-cache-refresh", daemon=True).start()


# Stale-while-revalidate refreshes wait their turn on two shared threads. A page
# or a verify pass that finds many stale routes must not start every collector
# at once: together they starve request threads of the GIL. Two, so one slow
# or hung collector does not hold back every other refresh.
_SWR_REFRESH_WORKERS = 2
_swr_refresh_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_SWR_REFRESH_WORKERS, thread_name_prefix="swr-cache-refresh",
)
_swr_refresh_keys: set[str] = set()


def _queue_swr_refresh(key: str, ttl: float, compute: Callable[[], object]) -> None:
    """Queue one refresh per key; it skips the work if a fresh value arrived meanwhile."""

    def _run() -> None:
        try:
            cache_get_or_compute(key, ttl, compute)
        except Exception:
            _cache_log.debug("stale-while-revalidate refresh failed", exc_info=True)
        finally:
            with _refresh_keys_lock:
                _swr_refresh_keys.discard(key)

    with _refresh_keys_lock:
        if key in _swr_refresh_keys:
            return
        _swr_refresh_keys.add(key)
    _swr_refresh_executor.submit(_run)


class _Unstorable(Exception):
    """A computed value that reports its own failure: served to its reader, never stored."""

    def __init__(self, value: object) -> None:
        super().__init__("value reports a failure")
        self.value = value


async def cache_read_swr(  # noqa: UP047 — ruff pyflakes lacks PEP 695 type-param support here
    key: str, ttl: float, compute: Callable[[], T], *, storable: Callable[[T], bool] | None = None,
) -> tuple[T, float, bool]:
    """Stale-while-revalidate read: the stored value at once, with its true age.

    Returns ``(value, age, stored)``. A value older than ``ttl`` is still
    returned, with its real age, and one background refresh is queued on the
    shared refresh threads. A value that went unread past
    :data:`DEMAND_WINDOW_S` after it expired is dormant: that read starts (or
    joins) one refresh and waits for it, off the event loop, up to
    :data:`SWR_DORMANT_READ_WAIT_S`; a slower refresh leaves it the stored value
    with its true age. Only the first read of a key waits for the whole
    computation, which concurrent first reads share, for at most
    :data:`SWR_DORMANT_READ_WAIT_S`; a slower first computation keeps running
    and stores its value, and the read gets the typed warming 503 meanwhile.
    The age is the time since the value was stored.

    A refresh that fails, or whose value ``storable`` rejects (the value reports
    its own failure), leaves the previous value in place, still ageing. When
    nothing is stored yet, such a value is returned with ``stored`` False and
    is not kept.
    """

    def guarded() -> T:
        value = compute()
        if storable is not None and not storable(value):
            raise _Unstorable(value)
        return value

    try:
        stored = cache_get_stored(key)
        if stored is not None and stored[1] >= ttl + DEMAND_WINDOW_S:
            # This read starts or joins the refresh itself; nothing more to queue.
            dormant = await asyncio.to_thread(cache_get_or_refresh, key, ttl, guarded, wait_s=SWR_DORMANT_READ_WAIT_S)
            if dormant is not None:
                return dormant[0], dormant[1], True  # type: ignore[return-value]
            stored = None
        if stored is None:
            try:
                # The computation is shared and shielded: a read that stops waiting leaves it running.
                first = await asyncio.wait_for(
                    cache_get_or_compute_async(key, ttl, guarded), timeout=SWR_DORMANT_READ_WAIT_S,
                )
            except TimeoutError:
                raise warming_unavailable("snapshot warming") from None
            stored = cache_get_stored(key)
            return first, 0.0 if stored is None else stored[1], True
    except _Unstorable as failed:
        return failed.value, 0.0, False  # type: ignore[return-value]
    if stored[1] >= ttl:
        _queue_swr_refresh(key, ttl, guarded)
    return stored[0], stored[1], True  # type: ignore[return-value]


def reports_failure(value: object) -> bool:
    """Whether a route payload reports its own failure (``error``, or an unusable ``availability``)."""
    if not isinstance(value, dict):
        return False
    return bool(value.get("error")) or value.get("availability") in _FAILED_AVAILABILITY


_FAILED_AVAILABILITY = frozenset({"unavailable", "malformed", "schema_read_failed", "db_missing", "db_error"})


def swr_route(
    name: str, ttl: float, *, vary: tuple[str, ...] = (),
) -> Callable[[Callable[..., T]], Callable[..., Awaitable[T]]]:
    """Serve a synchronous read-only GET handler through :func:`cache_read_swr`.

    Query parameters named in ``vary`` (each with a bounded set of values) are
    part of the cache key. A call that sets any other parameter away from its
    default is not cached: it runs the handler as before. Every cached
    response carries the snapshot freshness headers with the value's true age
    (``X-Snapshot-Age-S``), marked ``stale`` past ``ttl``. A payload that
    reports its own failure (:func:`reports_failure`) is never stored: a failed
    refresh leaves the last good value ageing, and a failed first read is
    returned as ``unavailable``.
    """

    def decorate(handler: Callable[..., T]) -> Callable[..., Awaitable[T]]:
        if inspect.iscoroutinefunction(handler):
            raise TypeError(f"swr_route needs a synchronous handler: {handler.__name__}")
        signature = inspect.signature(handler)
        if "ctx" not in signature.parameters:
            raise TypeError(f"swr_route needs a ctx parameter: {handler.__name__}")
        unknown = set(vary) - set(signature.parameters)
        if unknown:
            raise TypeError(f"swr_route vary names no parameter of {handler.__name__}: {sorted(unknown)}")
        defaults = {
            param.name: getattr(param.default, "default", param.default)
            for param in signature.parameters.values()
            if param.name != "ctx" and param.name not in vary
        }

        @functools.wraps(handler)
        async def wrapper(*, _swr_response: Response, **kwargs: Any) -> T:
            if any(kwargs.get(param) != default for param, default in defaults.items()):
                return await asyncio.to_thread(functools.partial(handler, **kwargs))
            ctx = kwargs["ctx"]
            # Every root, not just the project root: two contexts never share a value.
            key = await asyncio.to_thread(
                ctx_scoped_ttl_key, ctx, "swr", name, repr(ctx.roots),
                *(f"{param}={kwargs.get(param)!r}" for param in vary),
            )
            value, age, stored = await cache_read_swr(
                key, ttl, functools.partial(handler, **kwargs), storable=lambda value: not reports_failure(value),
            )
            _swr_response.headers.update(snapshot_headers(age, ttl, unavailable=not stored))
            return value

        response_param = inspect.Parameter("_swr_response", inspect.Parameter.KEYWORD_ONLY, annotation=Response)
        wrapper.__signature__ = signature.replace(  # type: ignore[attr-defined]
            parameters=[*signature.parameters.values(), response_param],
        )
        return wrapper

    return decorate


def cache_get_or_refresh(key: str, ttl: float, compute: Callable[[], object], *, wait_s: float) -> tuple[object, float] | None:
    """TTL read whose recompute never holds a request longer than ``wait_s``.

    A value younger than ``ttl`` is returned at once with its age. Otherwise the
    caller starts (or joins) one background flight and waits at most ``wait_s``
    for it. If the flight is still running, or fails, the previous value is
    returned with its real age (stale-while-revalidate); with no previous value
    a running flight returns ``None`` so the caller can report a warming state,
    and a failed flight raises. The flight keeps running and stores its result.
    """
    stored = cache_get_stored(key)
    if stored is not None and stored[1] < ttl:
        return stored
    fut, leader, cached = _claim_flight(key, ttl, force=False)
    if cached is not None:
        return cached, 0.0
    assert fut is not None
    if leader:

        def _lead() -> None:
            with suppress(Exception):
                _finish_flight(key, fut, compute)

        threading.Thread(target=_lead, name="status-cache-refresh", daemon=True).start()
    try:
        return fut.result(timeout=wait_s), 0.0
    except TimeoutError:
        # Re-read so the age includes the wait (or the flight finished just now).
        return cache_get_stored(key)
    except Exception:
        if stored is None:
            raise
        _cache_log.debug("cache refresh failed; serving the previous value", exc_info=True)
        return cache_get_stored(key)


def cache_invalidate(prefix: str = "") -> int:
    """Drop every cache entry whose key starts with ``prefix``.

    With ``prefix=""`` (default) the whole cache is cleared. Returns
    the number of entries removed. Useful for ``?fresh=true`` bypass
    paths and for tests that want a clean slate between cases.
    """
    with _ttl_cache_lock:
        keys = [k for k in _ttl_cache if k.startswith(prefix)]
        for key in keys:
            _ttl_cache.pop(key, None)
        if prefix == "":
            _retained.clear()
        else:
            dropped = [
                item
                for item, current in _retained.items()
                if item.startswith(prefix) or current.startswith(prefix)
            ]
            for item in dropped:
                _retained.pop(item, None)
        return len(keys)
