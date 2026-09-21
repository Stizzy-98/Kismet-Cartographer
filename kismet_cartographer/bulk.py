"""Threaded _bulk writer.

* `index` op (default) with a deterministic `_id` => re-ingesting overwrites the same documents
  instead of duplicating them. `create` op instead skips documents that already exist (409).
* Per-item retry (with backoff) for 429/503; everything else that fails is counted and handed to
  `reject_sink` so nothing disappears silently.
* Bounded in-flight batches, so memory stays flat no matter how large the capture file is.
"""
from __future__ import annotations

import json
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from .esclient import Client

Item = Tuple[str, str, str, str]  # (index, id, action_json, doc_json)


class BulkIndexer:
    def __init__(self, client: Client, *, workers: int = 2, batch_docs: int = 5000, batch_bytes: int = 8_000_000,
                 op: str = "index", reject_sink: Optional[Callable[[Dict[str, Any]], None]] = None):
        if op not in ("index", "create"):
            raise ValueError("op must be 'index' or 'create'")
        self.client, self.op = client, op
        self.batch_docs, self.batch_bytes = batch_docs, batch_bytes
        self.reject_sink = reject_sink
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="bulk")
        self._sem = threading.BoundedSemaphore(max(1, workers) * 2)
        self._lock = threading.Lock()
        self._buf: List[Item] = []
        self._buf_bytes = 0
        self._futures: List[Future] = []
        self._fatal: Optional[BaseException] = None
        self.indexed = 0
        self.existing = 0
        self.failed = 0
        self.by_index: Counter = Counter()
        self.failure_examples: List[str] = []

    # ------------------------------------------------------------------ public
    def add(self, index: str, doc_id: str, doc: Dict[str, Any]) -> None:
        if self._fatal:
            raise self._fatal
        action = json.dumps({self.op: {"_index": index, "_id": doc_id}}, separators=(",", ":"))
        body = json.dumps(doc, separators=(",", ":"), ensure_ascii=True)
        self._buf.append((index, doc_id, action, body))
        self._buf_bytes += len(action) + len(body) + 2
        if len(self._buf) >= self.batch_docs or self._buf_bytes >= self.batch_bytes:
            self._submit()

    def flush(self) -> None:
        if self._buf:
            self._submit()
        for f in self._futures:
            f.result()  # re-raises a worker's fatal error (auth failure, connection loss, ...)
        self._futures.clear()
        if self._fatal:
            raise self._fatal

    def close(self) -> None:
        self._pool.shutdown(wait=True)

    # ----------------------------------------------------------------- internals
    def _submit(self) -> None:
        items, self._buf, self._buf_bytes = self._buf, [], 0
        self._sem.acquire()
        fut = self._pool.submit(self._send, items)
        fut.add_done_callback(lambda f: self._sem.release())
        self._futures = [f for f in self._futures if not f.done()] + [fut]

    def _send(self, items: List[Item]) -> None:
        attempt = 0
        try:
            while items:
                body = ("\n".join(f"{a}\n{d}" for (_, _, a, d) in items) + "\n").encode("utf-8")
                resp = self.client.bulk(body)
                results = resp.get("items") or []
                if not resp.get("errors"):
                    with self._lock:
                        self.indexed += len(items)
                        for it in items:
                            self.by_index[it[0]] += 1
                    return
                retry: List[Item] = []
                ok = exists = failed = 0
                for it, res in zip(items, results):
                    r = next(iter(res.values()))
                    status = int(r.get("status", 0))
                    if status < 300:
                        ok += 1
                        with self._lock:
                            self.by_index[it[0]] += 1
                    elif status == 409 and self.op == "create":
                        exists += 1
                    elif status in (429, 503) and attempt < 6:
                        retry.append(it)
                    else:
                        failed += 1
                        self._reject(it, status, r.get("error"))
                with self._lock:
                    self.indexed += ok
                    self.existing += exists
                    self.failed += failed
                items = retry
                attempt += 1
                if items:
                    time.sleep(min(30.0, 0.5 * 2 ** attempt))
            return
        except BaseException as e:  # noqa: BLE001 - surfaced to the producer thread
            with self._lock:
                self._fatal = self._fatal or e
            raise

    def _reject(self, it: Item, status: int, error: Any) -> None:
        reason = error.get("reason") if isinstance(error, dict) else str(error)
        etype = error.get("type") if isinstance(error, dict) else ""
        with self._lock:
            if len(self.failure_examples) < 5:
                self.failure_examples.append(f"{it[0]}/{it[1]}: HTTP {status} {etype}: {reason}")
        if self.reject_sink:
            self.reject_sink({"index": it[0], "id": it[1], "status": status, "error_type": etype, "reason": reason})
