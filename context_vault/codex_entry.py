"""显式 Codex hook 入口；内部适配器不扩展宿主原始 payload。"""
from __future__ import annotations

import json
import io
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class CodexInvocation:
    """只在本进程传递宿主元数据与请求，不写回宿主 stdin。"""
    def __init__(self, context, loaded, home, claim):
        self.context, self.loaded, self.home, self.claim = context, loaded, home, claim
        self.report = None

    def runtime_disabled(self):
        return os.environ.get("VAULT_LOADER_DISABLE") == "1" or (self.home / ".context-vault/.disabled").exists()

    def make_report(self):
        from context_vault import control
        if self.claim is None:
            return None
        owner = self
        try:
            core = control.loader_module("_session_ops")
        except Exception as exc:
            print("[context-vault] Codex control unavailable: " + type(exc).__name__, file=sys.stderr)
            return None

        class Report(core.TurnReport):
            def take_ops(self):
                if not self.request_seen or self.request_taken:
                    return self.ops
                self.request_taken = True
                self.ops = core.parse_request(owner.claim["core"], host="codex")
                self.rejected.extend(self.ops.rejected)
                return self.ops

            def finalize(self):
                if not self.request_seen or self.status == "duplicate":
                    return
                if sys.exc_info()[0] is not None:
                    self.status = "error"
                control.ack_control(owner.context, revision=owner.claim["revision"],
                                    report=self.to_snapshot(), home=owner.home)

        self.report = Report(event=self.context.event, session_id=self.context.session_id,
                      prompt_id=self.context.prompt_id, home=self.home,
                      request_seen=self.claim is not None,
                      metrics_on=bool((self.loaded.config.get("metrics", {}) or {}).get("enabled", False)))
        return self.report

    def filter_session_notes(self, notes):
        excluded = set(self.claim["core"].get("exclude", [])) if self.claim is not None else set()
        if self.claim is not None and self.report is not None:
            from context_vault.control import loader_module
            self.report.excluded = [{"path": note.path, "title": loader_module("_session_ops").note_title(note.path)}
                                    for note in notes if note.path in excluded]
        return [note for note in notes if note.path not in excluded]


def _reset_context(context, *, home):
    """只重置可再生上下文；调用数、已付字节、待并账统计与用户设置原样保留。"""
    from context_vault import control
    from context_vault.atomic import update_json
    state, fine = control.loader_module("_state"), control.loader_module("_fine_state")
    path = state.state_path_for_cwd(context.cwd)
    if path.exists():
        update_json(path, lambda old: dict(old, paths=[], fulltext_paths=[]), max_bytes=state.MAX_STATE_BYTES)
    topics = state.session_topics_path(context.cwd)
    if topics.exists():
        def remove(old):
            entries = old.get("topics")
            if isinstance(entries, dict):
                entries.pop(context.session_id, None)
            return old
        update_json(topics, remove, max_bytes=state.MAX_STATE_BYTES)
    path = fine.fine_path(context.session_id)
    if path is not None and path.exists():
        def reset(st):
            st.injected, st.supp, st.turn_started = [], None, {}
            return st
        fine.update_fine(context.session_id, reset)
    marker = fine.inflight_active(context.session_id, time.time())
    if marker is not None:
        fine.release_inflight(context.session_id, marker["gen_id"])


def run_hook(event: str, payload: Mapping[str, Any], *, home: Path) -> None:
    """所有异常 fail-open；精确身份、明确宿主与事件赢家先于控制请求读取。"""
    try:
        from context_vault import control
        from context_vault.events import claim_event
        from context_vault.host_config import effective_host_config
        from context_vault.runtime import HookContext, RuntimeKind
        from context_vault.session_source import resolve_current_session
        from context_vault import lifecycle
        from context_vault.atomic import atomic_write_json
        if event not in ("SessionStart", "UserPromptSubmit") or payload.get("hook_event_name") != event:
            return
        if home.resolve() != Path.home().resolve():
            raise ValueError("hook home does not match process identity")
        timestamp = datetime.now(timezone.utc).isoformat()
        ref = resolve_current_session(host="codex", context={
            "thread_id": payload.get("session_id"), "cwd": payload.get("cwd"),
            "transcript_path": payload.get("transcript_path"),
            "codex_home": os.environ.get("CODEX_HOME", str(home / ".codex"))}, home=home)
        if ref.actor_kind != "main":
            return
        raw_context = HookContext.from_payload(payload, runtime=RuntimeKind.CODEX)
        core_id = control.core_session_id(ref.thread_id)
        internal_turn = raw_context.event_id if raw_context.stable_event_id else "ss-" + uuid.uuid4().hex
        context = replace(raw_context, session_id=core_id, thread_id=ref.thread_id,
            root_session_id=ref.root_session_id, actor_kind="main", event_id=internal_turn)
        attached = control.has_attachment(context, home=home)
        runtime_off = os.environ.get("VAULT_LOADER_DISABLE") == "1" or (home / ".context-vault/.disabled").exists()
        if runtime_off and not attached:
            # 停用不读取 Vault，但压缩仍须使已经发出的旧代际结果失效。
            if (event == "SessionStart" and payload.get("source") in ("clear", "compact")
                    and lifecycle.known_thread(home=home, thread_id=ref.thread_id)):
                with lifecycle.locked_thread(home=home, thread_id=ref.thread_id) as (path, state):
                    state["epoch"] += 1
                    atomic_write_json(path, state)
                    context = replace(context, context_epoch=state["epoch"])
                    control.advance_epoch(context, home=home)
                    control.loader_module("_state").configure_context("codex", core_id)
                    _reset_context(context, home=home)
            return
        loader = control.loader_module("_config_loader")
        document = loader.load_config_document()
        config = effective_host_config(document.raw, host="codex", base=document.loaded.config)
        config.session_ref, config.trigger_timestamp = ref, timestamp
        config.state_session_id, config.context_epoch = core_id, 0
        loaded = loader.ConfigLoad(config, document.loaded.fallback_reason, document.loaded.detail)
        module = control.loader_module("session_start_load" if event == "SessionStart" else "prompt_submit_load")
        section = "session_start" if event == "SessionStart" else "user_prompt_submit"
        disabled = (runtime_off or not config.get("enabled", True)
            or not config.get("runtimes", {}).get("codex", {}).get("enabled", True)
            or not config.get(section, {}).get("enabled", True)
            or module._is_opt_out_path(ref.cwd, config.get("opt_out_paths", []))
            or control.loader_module("_signal_collect").collect_signal_i_project_claude_md(ref.cwd).disabled)
        reset = event == "SessionStart" and payload.get("source") in ("clear", "compact")
        if disabled and not attached and not (reset and lifecycle.known_thread(home=home, thread_id=ref.thread_id)):
            return
        if not claim_event("codex", event, ref.thread_id, raw_context.event_id, scope=str(ref.cwd),
            stable=raw_context.stable_event_id, ttl_seconds=None if raw_context.stable_event_id else 5.0, home=home):
            return
        with lifecycle.locked_thread(home=home, thread_id=ref.thread_id) as (path, state):
            if reset:
                state["epoch"] += 1
            if event == "SessionStart":
                state["start_seq"] = state.get("start_seq", 0) + 1
            else:
                state["last_turn_id"] = context.event_id
            atomic_write_json(path, state)
            context = replace(context, context_epoch=state["epoch"])
            config.context_epoch = state["epoch"]
            control.advance_epoch(context, home=home)
            # 栅栏只围住本地核心；精排模型在 detached 子进程、锁外运行。
            control.loader_module("_state").configure_context("codex", core_id)
            if reset:
                _reset_context(context, home=home)
            desktop = ref.originator == 'Codex Desktop' and not disabled
            bootstrap = desktop and event == 'SessionStart' and context.source in ('startup', 'resume', 'fork', 'clear')
            if bootstrap:
                control.begin_bootstrap_capture(context, home=home)
            normalized = {"hook_event_name": event, "session_id": core_id, "cwd": str(ref.cwd),
                "prompt": payload.get("prompt", ""), "source": payload.get("source", ""),
                "transcript_path": str(ref.transcript_path)}
            allow_capture = desktop
            if event == "UserPromptSubmit":
                normalized["prompt_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "context-vault/turn/" + internal_turn))
                # 与核心来源闸门共用判据；系统通知不消耗首次真实提问的捕获额度。
                allow_capture = desktop and not (
                    config.get("relevance", {}).get("skip_non_user_prompts", True)
                    and module._is_system_injected_prompt(normalized, normalized["prompt"]))
            claim = control.claim_control(context, home=home, allow_bootstrap=allow_capture)
            invocation = CodexInvocation(context, loaded, home, claim)
            if bootstrap:
                from context_vault.panel_bootstrap import bootstrap_text, decorate_output
                captured = io.StringIO()
                try:
                    with redirect_stdout(captured):
                        module.main(normalized, adapter=invocation)
                finally:
                    raw = captured.getvalue()
                    try:
                        result = decorate_output(raw, bootstrap_text(ref))
                    except Exception:
                        print('[context-vault] bootstrap output unavailable', file=sys.stderr)
                        result = raw
                    sys.stdout.write(result)
            else:
                module.main(normalized, adapter=invocation)
    except Exception as exc:
        # 不打印 payload / prompt / 配置值，避免诊断变成敏感内容回显。
        print("[context-vault] Codex hook degraded: " + type(exc).__name__, file=sys.stderr)


if __name__ == "__main__":
    try:
        from context_vault.exe_search import harden_exe_search
        harden_exe_search()
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) <= 2 * 1024 * 1024:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                run_hook(payload.get("hook_event_name", ""), payload, home=Path.home())
    except Exception:
        print("[context-vault] Codex hook unavailable", file=sys.stderr)
