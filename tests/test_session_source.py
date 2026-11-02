"""真实结构的合成 JSONL；不含账号、真实会话或凭据。"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from context_vault.session_source import SessionRef, _under, read_session, recent_prompts, resolve_current_session


def _row(kind, payload, sec=0):
    return {"type": kind, "timestamp": f"2026-10-20T00:00:{sec:02d}Z", "payload": payload}


def _message(role, text, sec=1):
    return _row("response_item", {"type": "message", "role": role, "content": [
        {"type": "input_text" if role == "user" else "output_text", "text": text}]}, sec)


def _file(home, sid="thread-a", extra=()):
    path = home / ".codex/sessions/2026/10/20" / ("rollout-example-" + sid + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [_row("session_meta", {"id": sid, "cwd": str(home / "project"), "source": "vscode", "thread_source": "user"})]
    rows.extend(extra)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return path


def _resolve(home, path=None, sid="thread-a"):
    context = {"thread_id": sid, "cwd": str(home / "project")}
    if path:
        context["transcript_path"] = str(path)
    return resolve_current_session(host="codex", context=context, home=home)


def test_codex_uses_message_carrier_without_mirrors_or_internal_content(tmp_path):
    path = _file(tmp_path, extra=[_message("user", "查询设计方案"),
        _row("event_msg", {"type": "user_message", "message": "查询设计方案"}, 1),
        _message("developer", "不得进入摘要"), _message("system", "系统内容"),
        _row("response_item", {"type": "reasoning", "encrypted_content": "secret"}),
        _row("response_item", {"type": "function_call_output", "output": "工具内容"}),
        _message("user", "# AGENTS.md instructions for project\n宿主规则"),
        _message("user", "<skill>技能文档</skill>"), _message("assistant", "答复设计", 2)])
    ref = _resolve(tmp_path, path)
    got = read_session(ref)
    assert [m["text"] for m in got.messages] == ["查询设计方案", "答复设计"]
    assert got.completeness == "complete" and ref.actor_kind == "main"


def test_exact_thread_and_first_metadata_win_over_newer_other_thread(tmp_path):
    child = _file(tmp_path, extra=[_row("session_meta", {"id": "parent-thread", "cwd": "/other"}),
                                   _message("user", "当前线程的问题")])
    other = _file(tmp_path, "thread-b", [_message("user", "另一个线程")])
    os.utime(other, (2000000000, 2000000000))
    ref = _resolve(tmp_path)
    assert ref.transcript_path == child.resolve()
    assert ref.thread_id == ref.root_session_id == "thread-a"
    assert [m["text"] for m in read_session(ref).messages] == ["当前线程的问题"]


def test_missing_or_wrong_identity_cannot_resolve_current(tmp_path):
    p = _file(tmp_path, "thread-b")
    with pytest.raises(ValueError):
        resolve_current_session(host="codex", context={"cwd": str(tmp_path)}, home=tmp_path)
    with pytest.raises(ValueError):
        _resolve(tmp_path, p)


def test_recent_prompts_cut_off_future_at_trigger_time(tmp_path):
    p = _file(tmp_path, extra=[_message("user", "过去的输入", 1), _message("user", "当前输入", 2),
                              _message("user", "未来输入不能进入", 3)])
    ref = _resolve(tmp_path, p)
    assert recent_prompts(ref, before="2026-10-20T00:00:02Z", limit=5) == ["过去的输入"]


def test_bad_record_reports_incomplete(tmp_path):
    p = _file(tmp_path, extra=[_message("user", "可读取的输入")])
    with p.open("a", encoding="utf-8") as f:
        f.write("{broken\n")
    got = read_session(_resolve(tmp_path, p))
    assert got.completeness == "incomplete"
    assert "malformed_record" in got.reasons


@pytest.mark.parametrize("extended_input", [False, True] if os.name == "nt" else [False])
def test_source_cannot_escape_session_roots_through_link(tmp_path, extended_input):
    p = _file(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = outside / p.name
    p.replace(moved)
    link = p.parent / "linked"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    candidate = link / moved.name
    if extended_input:
        candidate = Path("\\\\?\\" + str(candidate.absolute()))
    with pytest.raises(ValueError):
        _resolve(tmp_path, candidate)


@pytest.mark.skipif(os.name != "nt", reason="真实 Windows 文件名称等价验证")
@pytest.mark.parametrize("extended_source,extended_home", [(False, False), (False, True), (True, False), (True, True)])
def test_windows_source_names_resolve_same_identity_and_keep_io_path(tmp_path, monkeypatch, extended_source, extended_home):
    for key in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(key, str(tmp_path))
    source = _file(tmp_path, extra=[_message("user", "合成路径验证")])
    transcript = Path("\\\\?\\" + str(source.resolve())) if extended_source else source
    directory = tmp_path / ".codex"
    codex_home = Path("\\\\?\\" + str(directory.resolve())) if extended_home else directory
    assert transcript.samefile(source)
    ref = resolve_current_session(host="codex", context={"thread_id": "thread-a",
        "transcript_path": str(transcript), "codex_home": str(codex_home)}, home=tmp_path)
    assert ref.transcript_path == transcript.resolve(), "读取仍保留原始 resolved Path"
    assert ref.thread_id == "thread-a" and ref.actor_kind == "main"
    assert read_session(ref).messages[0]["text"] == "合成路径验证"
    # 控制入口会再次验证来源，不能只让首次 resolver 通过。
    from context_vault.control import local_state
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert local_state(ref, home=tmp_path)["thread_id"] == "thread-a"


@pytest.mark.skipif(os.name != "nt", reason="真实 Windows 来源拒绝路径")
@pytest.mark.parametrize("invalid", ["outside", "identity", "filename"])
def test_extended_source_still_rejects_untrusted_location_and_identity(tmp_path, invalid):
    source = _file(tmp_path)
    if invalid == "outside":
        target = tmp_path / "outside" / source.name
        target.parent.mkdir()
        target.write_bytes(source.read_bytes())
        source = target
    elif invalid == "identity":
        row = json.loads(source.read_text(encoding="utf-8"))
        row["payload"]["id"] = "other-thread"
        source.write_text(json.dumps(row), encoding="utf-8")
    else:
        source = source.rename(source.with_name("not-a-rollout.jsonl"))
    with pytest.raises(ValueError, match="outside verified roots"):
        _resolve(tmp_path, Path("\\\\?\\" + str(source.resolve())))


@pytest.mark.parametrize("candidate,root,allowed", [
    (r"\\?\C:\safe\sessions\rollout-a.jsonl", r"C:\safe\sessions", True),
    (r"C:\safe\sessions\rollout-a.jsonl", r"\\?\C:\safe\sessions", True),
    (r"\\?\UNC\server\share\sessions\rollout-a.jsonl", r"\\server\share\sessions", True),
    (r"\\server\share\sessions\rollout-a.jsonl", r"\\?\UNC\server\share\sessions", True),
    (r"\\?\C:\safe\sessions", r"C:\safe\sessions", False),
    (r"\\?\C:\safe\sessions-other\rollout-a.jsonl", r"C:\safe\sessions", False),
    (r"\\?\UNC\server\share-other\sessions\rollout-a.jsonl", r"\\server\share\sessions", False),
    (r"\\.\C:\safe\sessions\rollout-a.jsonl", r"C:\safe\sessions", False),
    (r"\\?\GLOBALROOT\Device\HarddiskVolume1\safe\sessions\rollout-a.jsonl", r"C:\safe\sessions", False),
    (r"\\?\C:relative\rollout-a.jsonl", r"C:\safe\sessions", False),
])
def test_windows_comparison_names_preserve_containment(candidate, root, allowed):
    # UNC 只验证命名规则；不声称连接了真实 SMB 服务器。
    assert _under(PureWindowsPath(candidate), PureWindowsPath(root)) is allowed


def test_posix_literal_backslashes_are_not_windows_names():
    assert not _under(PurePosixPath(r"\\?\C:\safe\sessions\rollout-a.jsonl"), PurePosixPath("C:/safe/sessions"))


def test_claude_messages_and_typed_recent_prompts_remain_available(tmp_path):
    p = tmp_path / "claude.jsonl"
    rows = [{"type": "user", "promptSource": "typed", "timestamp": "2026-10-20T00:00:01Z",
             "message": {"role": "user", "content": "Claude 手动输入"}},
            {"type": "assistant", "timestamp": "2026-10-20T00:00:02Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "Claude 答复"}]}},
            {"type": "user", "promptSource": "sdk", "timestamp": "2026-10-20T00:00:03Z",
             "message": {"role": "user", "content": "SDK 生成输入"}}]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    ref = SessionRef("claude", "thread", "thread", p, tmp_path)
    assert [m["text"] for m in read_session(ref).messages] == ["Claude 手动输入", "Claude 答复", "SDK 生成输入"]
    assert recent_prompts(ref, before="2026-10-20T00:00:05Z", limit=3) == ["Claude 手动输入"]


def test_modification_attribution_does_not_claim_unknown_tools_are_no_changes(tmp_path):
    patch = "*** Begin Patch\n*** Update File: source.py\n@@\n-old\n+new\n*** End Patch"
    p = _file(tmp_path, extra=[_row("response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": "call-a", "input": patch}),
        _row("response_item", {"type": "custom_tool_call_output", "call_id": "call-a", "output": "Success. Updated the following files:\nM source.py"}),
        _row("response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "call-b", "input": "await tools.exec_command({cmd: 'unknown'});"})])
    got = read_session(_resolve(tmp_path, p))
    assert got.touched_files == [tmp_path / "project/source.py"]
    assert got.metadata["touched_files_status"] == "unknown"


@pytest.mark.parametrize("output", ['{}', '{"isError":true,"text":"patch failed"}'])
def test_exec_completion_without_patch_success_is_unknown(tmp_path, output):
    patch = "*** Begin Patch\n*** Add File: never.py\n+pass\n*** End Patch"
    script = "await tools.apply_patch(" + json.dumps(patch) + ");"
    p = _file(tmp_path, extra=[_row("response_item", {"type": "custom_tool_call", "name": "exec",
        "call_id": "call-a", "input": script}), _row("response_item", {"type": "custom_tool_call_output",
        "call_id": "call-a", "output": [{"type": "text", "text": "Script completed\nOutput:\n" + output}]})])
    got = read_session(_resolve(tmp_path, p))
    assert got.touched_files == []
    assert got.metadata["touched_files_status"] == "unknown"


def test_unexecuted_patch_in_branch_is_not_attributed(tmp_path):
    patch = "*** Begin Patch\n*** Add File: never.py\n+pass\n*** End Patch"
    script = "if (false) { await tools.apply_patch(" + json.dumps(patch) + "); }"
    p = _file(tmp_path, extra=[_row("response_item", {"type": "custom_tool_call", "name": "exec",
        "call_id": "call-a", "input": script}), _row("response_item", {"type": "custom_tool_call_output",
        "call_id": "call-a", "output": [{"type": "text", "text": "Script completed\nOutput:\n"}]})])
    got = read_session(_resolve(tmp_path, p))
    assert got.touched_files == []
    assert got.metadata["touched_files_status"] == "unknown"
