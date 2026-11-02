"""栅栏本身的异常传播、当前代际与路径隔离。"""
import json
import os

import pytest

from context_vault.atomic import atomic_write_json
from context_vault.lifecycle import current_epoch, locked_thread, result_guard


@pytest.mark.parametrize("exists", [False, True])
def test_guard_never_catches_the_callers_exception(tmp_path, exists):
    if exists:
        with locked_thread(home=tmp_path, thread_id="thread-a") as (path, state):
            atomic_write_json(path, state)
    with pytest.raises(OSError, match="^body-probe$"):
        with result_guard({"codex_context": {"thread_id": "thread-a", "epoch": 0}}, home=tmp_path) as current:
            assert current is exists
            raise OSError("body-probe")


def test_only_current_epoch_can_commit(tmp_path):
    with locked_thread(home=tmp_path, thread_id="thread-a") as (path, state):
        state["epoch"] = 2
        atomic_write_json(path, state)
    assert current_epoch(home=tmp_path, thread_id="thread-a") == 2
    for epoch, want in ((0, False), (1, False), (2, True), (3, False)):
        with result_guard({"codex_context": {"thread_id": "thread-a", "epoch": epoch}}, home=tmp_path) as current:
            assert current is want


def test_lifecycle_root_cannot_redirect_to_another_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / ".context-vault/state/codex/v2"
    root.mkdir(parents=True)
    link = root / "lifecycle"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="linked lifecycle"):
        with locked_thread(home=tmp_path, thread_id="thread-a"):
            pytest.fail("linked root was entered")
    assert list(outside.iterdir()) == []
