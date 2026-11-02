"""在飞终结清理：瞬时读删竞争、重试上限、逐次 owner 核对与真实 worker 接线。"""
from __future__ import annotations

import json
import os
import threading
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import _fine_state as fs, _state, _topic
from scripts._config_loader import DEFAULT_CONFIG

SID = "inflight-release-test"
GEN = "0123456789abcdef"


@pytest.fixture(params=["legacy", "claude", "codex"])
def marker(tmp_home, request, monkeypatch):
    _state.adopt_runtime(request.param)
    monkeypatch.setattr(fs, "_WARNED", set())
    assert fs.claim_inflight(SID, GEN, 120, time.time())
    return fs.inflight_path(SID)


def _clock(monkeypatch, sleep):
    # 仅替换被测模块的时钟，不改进程共享的 time.sleep。
    monkeypatch.setattr(fs, "time", SimpleNamespace(time=time.time, sleep=sleep))


def test_inflight_release_retries_transient_unlink(marker, monkeypatch):
    real = Path.unlink
    attempts, sleeps = [], []

    def busy_twice(path, *args, **kwargs):
        if path == marker:
            attempts.append(json.loads(path.read_text(encoding="utf-8"))["gen_id"])
            if len(attempts) < 3:
                raise PermissionError("temporary reader")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", busy_twice)
    _clock(monkeypatch, sleeps.append)
    fs.release_inflight(SID, GEN)
    assert not marker.exists(), "瞬时占用解除后仍留下在飞标记"
    assert attempts == [GEN, GEN, GEN]
    assert sleeps == [0.02, 0.02]


def test_inflight_release_retries_unreadable_marker(marker, monkeypatch):
    real = Path.read_text
    reads, sleeps = [], []

    def unreadable_once(path, *args, **kwargs):
        if path == marker:
            reads.append(path)
            if len(reads) == 1:
                raise PermissionError("temporary read failure")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable_once)
    _clock(monkeypatch, sleeps.append)
    fs.release_inflight(SID, GEN)
    assert not marker.exists(), "不能把一次读失败永久当作换 owner"
    assert len(reads) == 2 and sleeps == [0.02]


def test_inflight_release_rechecks_owner_after_each_retry(marker, monkeypatch):
    real = Path.unlink
    attempts, sleeps = [], []
    newer = {"gen_id": "fedcba9876543210", "started": time.time(), "timeout": 120}

    def replaced_during_failure(path, *args, **kwargs):
        if path == marker:
            attempts.append(path)
            if len(attempts) == 1:
                marker.write_text(json.dumps(newer), encoding="utf-8")
                raise PermissionError("owner replaced during retry")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", replaced_during_failure)
    _clock(monkeypatch, sleeps.append)
    fs.release_inflight(SID, GEN)
    assert marker.exists(), "重试误删了新 owner 的标记"
    assert json.loads(marker.read_text(encoding="utf-8")) == newer
    assert len(attempts) == 1 and sleeps == [0.02]


def test_inflight_release_retries_failed_stat_without_assuming_missing(marker, monkeypatch):
    real = os.lstat
    calls, sleeps = [], []

    def unreadable_once(path, *args, **kwargs):
        if Path(path) == marker:
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError("temporary stat failure")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", unreadable_once)
    _clock(monkeypatch, sleeps.append)
    fs.release_inflight(SID, GEN)
    assert not marker.exists() and sleeps == [0.02]


def test_inflight_release_disappearing_before_unlink_is_success(marker, monkeypatch):
    real = Path.unlink
    sleeps = []

    def disappeared(path, *args, **kwargs):
        if path == marker:
            real(path)
            raise FileNotFoundError("already released")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", disappeared)
    _clock(monkeypatch, sleeps.append)
    fs.release_inflight(SID, GEN)
    assert not marker.exists() and sleeps == []


def test_inflight_release_is_bounded_and_diagnoses_exhaustion(marker, monkeypatch, capsys):
    real = Path.unlink
    attempts, sleeps = [], []

    def always_busy(path, *args, **kwargs):
        if path == marker:
            attempts.append(path)
            assert len(attempts) <= 6, "测试桩自身限制失控重试"
            raise PermissionError("persistent reader")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", always_busy)
    _clock(monkeypatch, sleeps.append)
    fs.release_inflight(SID, GEN)
    assert marker.exists()
    assert len(attempts) == 5 and sleeps == [0.02] * 4
    assert "在飞标记清理重试耗尽" in capsys.readouterr().err


def test_inflight_release_corrupt_marker_is_bounded_and_never_deleted(marker, monkeypatch, capsys):
    marker.write_text("broken", encoding="utf-8")
    real_read, real_unlink = Path.read_text, Path.unlink
    reads, unlinks, sleeps = [], [], []

    def read(path, *args, **kwargs):
        if path == marker:
            reads.append(path)
            assert len(reads) <= 6, "测试桩自身限制失控读取"
        return real_read(path, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        if path == marker:
            unlinks.append(path)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "unlink", unlink)
    _clock(monkeypatch, sleeps.append)
    fs.release_inflight(SID, GEN)
    assert marker.exists() and unlinks == []
    assert len(reads) == 5 and sleeps == [0.02] * 4
    assert "在飞标记清理重试耗尽" in capsys.readouterr().err


def test_inflight_release_success_missing_and_wrong_owner_do_not_wait(marker, monkeypatch):
    sleeps = []
    _clock(monkeypatch, sleeps.append)
    fs.release_inflight(SID, "wrong")
    assert marker.exists()
    fs.release_inflight(SID, "")
    assert marker.exists()
    fs.release_inflight(SID, GEN)
    assert not marker.exists()
    fs.release_inflight(SID, GEN)
    assert sleeps == []


def test_inflight_release_after_real_read_handle_closes(marker, monkeypatch):
    retrying, reader_closed = threading.Event(), threading.Event()
    failures = []

    def pause(_delay):
        retrying.set()
        assert reader_closed.wait(2), "测试读句柄未按时关闭"

    def release():
        try:
            fs.release_inflight(SID, GEN)
        except BaseException as exc:
            failures.append(exc)

    _clock(monkeypatch, pause)
    worker = threading.Thread(target=release)
    try:
        with marker.open("r", encoding="utf-8") as reader:
            assert json.load(reader)["gen_id"] == GEN
            worker.start()
            if os.name == "nt":
                assert retrying.wait(2), "Windows真实读占用未触发清理重试"
    finally:
        reader_closed.set()
        if worker.ident is not None:
            worker.join(3)
    assert not worker.is_alive() and failures == []
    assert not marker.exists(), "真实读句柄关闭后未清理"
    if os.name != "nt":
        assert not retrying.is_set(), "POSIX读句柄不应导致删除等待"


def test_real_child_finally_retries_and_keeps_model_selection(
        marker, monkeypatch, tmp_path, tmp_vault, write_frontmatter_cache):
    from context_vault.host_config import HostConfig, ModelSelection

    runtime = _state.current_runtime()
    cfg = deepcopy(DEFAULT_CONFIG)
    cfg["vault_path"] = str(tmp_vault)
    selected = None if runtime == "codex" else "haiku"
    if runtime == "codex":
        cfg = HostConfig(cfg, host="codex", model_selection=ModelSelection(
            "codex", selected, "work", "custom", "runtime"))
    write_frontmatter_cache({"notes/recall.md": {
        "tags": ["召回"], "summary": "召回测试", "keywords": ["召回"], "mtime": 1}})
    slot = fs.claim_slot(GEN, 120, time.time())
    assert slot is not None
    payload = _topic.build_fine_payload(
        "pin", prompt="召回测试", config=cfg, gen_id=GEN, src_seq=1, slot=slot,
        transcript_path="", injected=[], excluded=[], prev_words=[], extra_paths=[])
    seen = []

    def fake_model(_text):
        seen.append(_topic._MODEL)
        return "PINNED: NONE\nWORDS: recall"

    real = Path.unlink
    attempts, sleeps = [], []

    def occupied_once(path, *args, **kwargs):
        if path == marker:
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError("worker cleanup reader")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(_topic, "_call_model", fake_model)
    monkeypatch.setattr(Path, "unlink", occupied_once)
    _clock(monkeypatch, sleeps.append)
    assert _topic.run_extraction_child(
        [str(tmp_path), SID, runtime], stdin_text=json.dumps(payload)) == 0
    assert seen == [selected]
    assert not marker.exists() and not (fs.slots_dir() / slot).exists()
    assert len(attempts) == 2 and sleeps == [0.02]
    stats = fs.load_fine(SID).stats_q
    assert len(stats) == 1 and stats[0]["gen_id"] == GEN and stats[0]["ok"] is True
