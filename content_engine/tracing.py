"""Small local traces: paired start/end/error JSON Lines events."""

import json
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import perf_counter
from uuid import uuid4

from pydantic import BaseModel
from langgraph.errors import GraphInterrupt


def _json_value(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot trace {type(value).__name__}")


class RunTrace:
    """Pass one instance through a run; no directory means tracing is disabled."""

    def __init__(self, directory: Path | None = None, *, secrets=(), console: bool = True) -> None:
        self.run_id = uuid4().hex
        self.path = directory / f"{self.run_id}.jsonl" if directory else None
        self.secrets = tuple(value for value in secrets if value)
        self.console = console
        self._parent = ContextVar(f"trace_parent_{self.run_id}", default=None)
        self._lock = Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))

    def _record(self, event: str, span_id: str, parent_id: str | None, name: str, kind: str, **data) -> None:
        record = {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "span_id": span_id,
            "parent_id": parent_id,
            "event": event,
            "name": name,
            "kind": kind,
            **data,
        }
        line = json.dumps(record, default=_json_value, ensure_ascii=False)
        for secret in self.secrets:
            line = line.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[redacted]")
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            if self.console:
                elapsed = f" {data['duration_ms']:.0f}ms" if "duration_ms" in data else ""
                print(f"[{self.run_id[:8]}] {event:5} {kind:10} {name}{elapsed}", file=sys.stderr, flush=True)

    @contextmanager
    def span(self, name: str, kind: str, *, inputs=None):
        """Fill the yielded dict with outputs/metadata; errors are recorded and re-raised."""
        details = {}
        if self.path is None:
            yield details
            return
        span_id, parent_id = uuid4().hex, self._parent.get()
        self._record("start", span_id, parent_id, name, kind, inputs=inputs)
        token = self._parent.set(span_id)
        started = perf_counter()
        try:
            yield details
        except GraphInterrupt:
            self._record("pause", span_id, parent_id, name, kind,
                         duration_ms=round((perf_counter() - started) * 1000, 2), **details)
            raise
        except Exception as error:
            self._record(
                "error", span_id, parent_id, name, kind,
                duration_ms=round((perf_counter() - started) * 1000, 2),
                error={"type": type(error).__name__, "message": str(error)},
                **details,
            )
            raise
        else:
            self._record(
                "end", span_id, parent_id, name, kind,
                duration_ms=round((perf_counter() - started) * 1000, 2),
                **details,
            )
        finally:
            self._parent.reset(token)
