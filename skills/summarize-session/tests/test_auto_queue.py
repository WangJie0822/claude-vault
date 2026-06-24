from __future__ import annotations
import json
import os
import threading
from pathlib import Path

from _auto_queue import (
    enqueue, list_queue, dequeue_by_session, increment_failure,
    QueueEntry,
)


def test_enqueue_appends_jsonl(tmp_path: Path):
    q = tmp_path / "queue.jsonl"
    enqueue(q, QueueEntry(
        session_id="s1", cwd="/c1",
        enqueued_at="2026-04-23T22:30:00+08:00",
        metrics={"total_messages": 10, "size_kb": 50.0,
                 "duration_min": 5, "has_edit_or_write": True},
        failure_count=0,
    ))
    enqueue(q, QueueEntry(
        session_id="s2", cwd="/c2",
        enqueued_at="2026-04-23T22:31:00+08:00",
        metrics={"total_messages": 20, "size_kb": 100.0,
                 "duration_min": 10, "has_edit_or_write": True},
        failure_count=0,
    ))
    lines = q.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["session_id"] == "s1"
    assert json.loads(lines[1])["session_id"] == "s2"


def test_list_queue_returns_entries_in_order(tmp_path: Path):
    q = tmp_path / "queue.jsonl"
    enqueue(q, QueueEntry(
        session_id="a", cwd="/", enqueued_at="2026-04-23T00:00:00+08:00",
        metrics={}, failure_count=0,
    ))
    enqueue(q, QueueEntry(
        session_id="b", cwd="/", enqueued_at="2026-04-23T00:01:00+08:00",
        metrics={}, failure_count=0,
    ))
    entries = list_queue(q)
    assert [e.session_id for e in entries] == ["a", "b"]


def test_list_queue_empty_or_missing(tmp_path: Path):
    assert list_queue(tmp_path / "missing.jsonl") == []
    q = tmp_path / "empty.jsonl"
    q.write_text("")
    assert list_queue(q) == []


def test_dequeue_by_session_atomic(tmp_path: Path):
    q = tmp_path / "queue.jsonl"
    for sid in ("a", "b", "c"):
        enqueue(q, QueueEntry(
            session_id=sid, cwd="/", enqueued_at="2026-04-23T00:00:00+08:00",
            metrics={}, failure_count=0,
        ))
    dequeue_by_session(q, {"b"})
    remaining = [e.session_id for e in list_queue(q)]
    assert remaining == ["a", "c"]


def test_increment_failure_keeps_others(tmp_path: Path):
    q = tmp_path / "queue.jsonl"
    enqueue(q, QueueEntry(
        session_id="x", cwd="/", enqueued_at="2026-04-23T00:00:00+08:00",
        metrics={}, failure_count=1,
    ))
    enqueue(q, QueueEntry(
        session_id="y", cwd="/", enqueued_at="2026-04-23T00:01:00+08:00",
        metrics={}, failure_count=0,
    ))
    increment_failure(q, "x")
    entries = {e.session_id: e for e in list_queue(q)}
    assert entries["x"].failure_count == 2
    assert entries["y"].failure_count == 0


def test_concurrent_enqueue_no_corruption(tmp_path: Path):
    q = tmp_path / "queue.jsonl"
    N = 50

    def worker(i: int):
        enqueue(q, QueueEntry(
            session_id=f"s{i}", cwd="/", enqueued_at=f"t{i}",
            metrics={"i": i}, failure_count=0,
        ))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = list_queue(q)
    assert len(entries) == N
    sids = sorted(e.session_id for e in entries)
    assert sids == sorted(f"s{i}" for i in range(N))
