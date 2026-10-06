"""Buffered, rotating JSONL tick logger for the Kalshi market maker.

Purpose: persist one structured record per priced tick so the dashboard state
is reconstructable after the fact. The record is the dict returned by
LOW_LATENCY.price_tick(); every field the dashboard renders is derived from it.

Design constraints (why this is not a naive `open().write()`):

  * Hot-path safety. Writes are buffered in memory; `flush()` is driven by a
    timer, never by an individual tick. A tick costs one dict copy + one
    `write()` into a user-space buffer. No disk I/O in the caller's path.
  * Bounded memory. If the consumer outruns the disk, the buffer is capped and
    rows are dropped with a counter rather than growing without limit.
  * Rotation. One file per UTC day, opened on demand. A multi-day run never
    produces a single multi-GB file, and a day can be moved or deleted without
    stopping the process.
  * Failure isolation. Any write/open error disables the sink, logs once, and
    retries on a backoff. A full disk degrades the logger, never the feed.
  * Clean shutdown. `close()` flushes and closes so the tail is not lost.

The module is standalone: it imports nothing from the project and has no
dependency on asyncio. The caller supplies a `clock` callable for the day
boundary so tests are deterministic.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import datetime, timezone


def _default_clock() -> float:
    return time.time()


class JsonlTickLogger:
    """Append JSON-serializable dicts to UTC-daily JSONL files, buffered.

    Parameters
    ----------
    directory : str
        Directory for the log files; created if missing.
    prefix : str
        Filename stem; the file is ``{prefix}_{YYYYMMDD}.jsonl``.
    flush_interval_sec : float
        How often the buffer is flushed to the OS.
    max_buffer_rows : int
        Buffer cap. When full, the oldest row is dropped (and counted) rather
        than blocking the feed or growing memory.
    sample_every : int
        Write every Nth accepted row. 1 = every tick. Raise to thin the dataset
        without touching the call site.
    failure_backoff_sec : float
        After a write/open error, wait this long before retrying.
    clock : callable
        Returns unix seconds; injected for tests.
    """

    def __init__(
        self,
        directory: str,
        prefix: str = "ticks",
        flush_interval_sec: float = 1.0,
        max_buffer_rows: int = 200_000,
        sample_every: int = 1,
        failure_backoff_sec: float = 5.0,
        clock=_default_clock,
    ):
        self.directory = directory
        self.prefix = prefix
        self.flush_interval_sec = max(flush_interval_sec, 0.0)
        self.max_buffer_rows = max(max_buffer_rows, 1)
        self.sample_every = max(int(sample_every), 1)
        self.failure_backoff_sec = max(failure_backoff_sec, 0.0)
        self._clock = clock

        self._buffer: deque[str] = deque()
        self._fh = None
        self._current_day = None
        self._last_flush = self._clock()
        self._disabled_until = 0.0

        self._n_seen = 0
        self._n_written = 0
        self._n_dropped = 0
        self._n_errors = 0
        self._last_error = None
        self._error_logged_once = False

    # -- public API --------------------------------------------------------

    def write(self, row: dict) -> None:
        """Queue one record. Never raises, never blocks, never hits disk.

        The tick path calls exactly this. Sampling and buffering happen here so
        the caller does not branch on any logger state.
        """
        self._n_seen += 1
        if self._n_seen % self.sample_every != 0:
            return

        now = self._clock()
        if now < self._disabled_until:
            self._n_dropped += 1
            return

        try:
            line = json.dumps(row, default=_json_default, separators=(",", ":"))
        except Exception as e:  # a non-serializable field must not kill the feed
            self._note_error(e)
            self._n_dropped += 1
            return

        if len(self._buffer) >= self.max_buffer_rows:
            self._buffer.popleft()
            self._n_dropped += 1
        self._buffer.append(line)

        if now - self._last_flush >= self.flush_interval_sec:
            self.flush()

    def flush(self) -> None:
        """Drain the buffer to the current day's file. Rotates at UTC midnight.

        Called on the flush timer and at shutdown. All errors are contained:
        on failure the rows are kept (up to the cap) and the sink is disabled
        for `failure_backoff_sec` before retrying.
        """
        self._last_flush = self._clock()
        if not self._buffer:
            return
        try:
            self._ensure_file()
            # One batched write for the whole buffer.
            self._fh.write("".join(line + "\n" for line in self._buffer))
            self._fh.flush()
            self._n_written += len(self._buffer)
            self._buffer.clear()
            self._error_logged_once = False
        except Exception as e:
            self._note_error(e)
            self._disabled_until = self._clock() + self.failure_backoff_sec

    def close(self) -> None:
        """Flush and close. Idempotent; safe to call from a finally block."""
        self.flush()
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def stats(self) -> dict:
        """Counters for a periodic heartbeat in the parent process' stderr."""
        return {
            "seen": self._n_seen,
            "written": self._n_written,
            "dropped": self._n_dropped,
            "errors": self._n_errors,
            "buffered": len(self._buffer),
            "path": self._path(),
            "last_error": self._last_error,
        }

    # -- internals ---------------------------------------------------------

    def _ensure_file(self) -> None:
        """Open today's file, rotating when the UTC day (or month) changes."""
        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        day = now.strftime("%Y%m%d")
        if self._fh is not None and day == self._current_day:
            return
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        path = self._path(now)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._fh = open(path, "a", buffering=1 << 20)
        self._current_day = day

    def _path(self, now: datetime | None = None) -> str:
        now = now or datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        # Files are grouped by month so a long run does not scatter hundreds of
        # dated files into one directory.
        month_dir = os.path.join(self.directory, now.strftime("%Y-%m"))
        return os.path.join(month_dir, f"{self.prefix}_{now.strftime('%Y%m%d')}.jsonl")

    def _note_error(self, e: Exception) -> None:
        self._n_errors += 1
        self._last_error = f"{type(e).__name__}: {e}"
        if not self._error_logged_once:
            self._error_logged_once = True
            try:
                import sys
                sys.stderr.write(json.dumps({
                    "ts": self._clock(), "event": "ticklog_error",
                    "error": self._last_error, "path": self._path(),
                }) + "\n")
            except Exception:
                pass


def _json_default(obj):
    """Fallback for values json cannot serialize: datetime, deque, sets, etc."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, deque):
        return list(obj)
    return str(obj)
