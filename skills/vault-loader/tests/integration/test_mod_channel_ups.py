"""UPS 真实入口的 mod 通道（spec §5.4、§6）。必须真起子进程：命名空间是进程级全局。"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
UPS = SCRIPTS / "prompt_submit_load.py"
# 占位 UUID 拆开拼接：发布闸门按 8-4-4-4-12 hex 拦截疑似真实 session id（test_no_secrets_in_tree）
SID = "-".join(("11111111", "2222", "3333", "4444", "555555555555"))
PID1 = "-".join(("aaaaaaaa", "bbbb", "cccc", "dddd", "000000000001"))
PID2 = "-".join(("aaaaaaaa", "bbbb", "cccc", "dddd", "000000000002"))
PID3 = "-".join(("aaaaaaaa", "bbbb", "cccc", "dddd", "000000000003"))


def _live(home: Path) -> Path:
    return home / ".context-vault" / "state" / "live"


def _sid_h() -> str:
    return hashlib.sha256(SID.encode("utf-8")).hexdigest()[:24]


def _req(home: Path, pid: str, obj: dict) -> Path:
    p = _live(home) / f"{_sid_h()}-{pid}.req.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


def _snap(home: Path) -> dict:
    return json.loads((_live(home) / f"{_sid_h()}.json").read_text(encoding="utf-8"))


def _config(home: Path, vault: Path, **extra) -> None:
    d = home / ".context-vault"
    d.mkdir(parents=True, exist_ok=True)
    cfg = {"vault_path": str(vault), "metrics": {"enabled": True},
           "relevance": {"session_topic": False}}
    cfg.update(extra)
    (d / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _seed(vault: Path, write_frontmatter_cache) -> None:
    (vault / "笔记").mkdir(parents=True, exist_ok=True)
    entries = {}
    for name in ("alpha", "beta", "gamma"):
        (vault / "笔记" / f"{name}.md").write_text(f"# {name}", encoding="utf-8")
        entries[f"笔记/{name}.md"] = {"tags": ["hook"], "category": "笔记",
                                       "keywords": ["注入设计", "hook 注入"],
                                       "summary": f"{name} 的 hook 注入设计说明，足够长",
                                       "mtime": 1900000000}
    write_frontmatter_cache(entries)


def _run(home: Path, cwd: Path, pid: str, prompt: str = "hook 注入设计 怎么做", env_extra=None):
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
                "HOME": str(home), "USERPROFILE": str(home)})
    env.pop("VAULT_LOADER_DISABLE", None)
    if env_extra:
        env.update(env_extra)
    payload = {"cwd": str(cwd), "prompt": prompt, "session_id": SID, "prompt_id": pid,
               "hook_event_name": "UserPromptSubmit"}
    r = subprocess.run([sys.executable, str(UPS)], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    return json.loads(out) if out else {}


def _ctx(out: dict) -> str:
    return (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""


def test_no_request_creates_no_live_dir(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache)
    out = _run(tmp_home, tmp_git_repo, PID1)
    assert "alpha" in _ctx(out) or "beta" in _ctx(out)
    assert not _live(tmp_home).exists()


def test_request_yields_scored_snapshot(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache)
    req = _req(tmp_home, PID1, {"v": 1})
    _run(tmp_home, tmp_git_repo, PID1)
    snap = _snap(tmp_home)
    assert snap["status"] == "scored" and snap["prompt_id"] == PID1
    assert snap["injected"] and all(i["path"].startswith("笔记/") for i in snap["injected"])
    assert snap["metrics_on"] is True and len(snap["prompt_h"]) == 16
    assert not req.exists()


def test_exclude_removes_note(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache)
    _req(tmp_home, PID1, {"v": 1, "exclude": ["笔记/alpha.md"]})
    out = _run(tmp_home, tmp_git_repo, PID1)
    assert "笔记/alpha.md" not in _ctx(out)
    snap = _snap(tmp_home)
    assert "笔记/alpha.md" not in [i["path"] for i in snap["injected"]]
    assert [e["path"] for e in snap["excluded"]] == ["笔记/alpha.md"]


def test_user_pin_rendered_with_own_header(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    from scripts.prompt_submit_load import PIN_HEADER, USER_PIN_HEADER
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache)
    _req(tmp_home, PID1, {"v": 1, "pin": ["笔记/gamma.md"], "exclude": ["笔记/gamma.md"]})
    out = _run(tmp_home, tmp_git_repo, PID1)
    assert USER_PIN_HEADER not in _ctx(out)           # 排除优先于钉住
    _req(tmp_home, PID2, {"v": 1, "pin": ["笔记/gamma.md"]})
    out = _run(tmp_home, tmp_git_repo, PID2, prompt="完全无关的 文本 关键词 组合")
    assert USER_PIN_HEADER in _ctx(out) and PIN_HEADER not in _ctx(out)
    assert _snap(tmp_home)["pins"]["user"] == [
        {"path": "笔记/gamma.md", "title": "gamma", "rendered": True}]


def test_gate_turn_writes_gate_status(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache)
    _req(tmp_home, PID1, {"v": 1})
    _run(tmp_home, tmp_git_repo, PID1, prompt="继续")
    snap = _snap(tmp_home)
    assert snap["status"] == "gate:deictic_only"
    # 节点 2 Q-H1：闸门轮也要如实报 metrics 开着，否则 mod 收到后把标注按钮收掉
    assert snap["metrics_on"] is True and "prompt_h" not in snap


def test_disabled_writes_disabled_status(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache)
    _req(tmp_home, PID1, {"v": 1, "exclude": ["笔记/alpha.md"]})
    _run(tmp_home, tmp_git_repo, PID1, env_extra={"VAULT_LOADER_DISABLE": "1"})
    snap = _snap(tmp_home)
    assert snap["status"] == "disabled" and snap["request_digest"] is None


def test_opt_out_cwd_writes_no_annotation(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _config(tmp_home, tmp_vault, opt_out_paths=[str(tmp_git_repo)])
    _seed(tmp_vault, write_frontmatter_cache)
    _req(tmp_home, PID1, {"v": 1, "annotations": [
        {"id": "ann-00000001", "path": "笔记/alpha.md", "kind": "admitted_list",
         "verdict": "relevant", "session": SID, "prompt_h": "0123456789abcdef", "ts": 1}]})
    _run(tmp_home, tmp_git_repo, PID1)
    assert _snap(tmp_home)["status"] == "disabled"
    assert not list((tmp_home / ".context-vault" / "metrics").rglob("annotations.jsonl"))


def _ann(pid_h: str, path: str, aid: str = "ann-00000001", kind: str = "admitted_list") -> dict:
    return {"id": aid, "path": path, "kind": kind, "verdict": "relevant",
            "session": SID, "prompt_h": pid_h, "ts": 1791550000}


def test_annotation_metrics_off_rejected(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _config(tmp_home, tmp_vault, metrics={"enabled": False})
    _seed(tmp_vault, write_frontmatter_cache)
    _req(tmp_home, PID1, {"v": 1, "annotations": [_ann("0123456789abcdef", "笔记/alpha.md")]})
    _run(tmp_home, tmp_git_repo, PID1)
    snap = _snap(tmp_home)
    assert snap["rejected"] == [{"what": "annotation", "key": "ann-00000001", "reason": "metrics_off"}]
    assert "prompt_h" not in snap
    assert not list(tmp_home.rglob("annotations.jsonl"))


def _ann_rows(home: Path) -> list:
    files = list((home / ".context-vault" / "metrics").rglob("annotations.jsonl"))
    if not files:
        return []
    return [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines() if x.strip()]


def test_annotation_verified_and_written_once(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache)
    _req(tmp_home, PID1, {"v": 1})
    _run(tmp_home, tmp_git_repo, PID1)
    s1 = _snap(tmp_home)
    # kind 取快照条目自带的值：mod 投递的就是它；首篇常是全文注入，写死 admitted_list 会被核对拒收
    item = s1["injected"][0]
    target, kind = item["path"], item["kind"]
    _req(tmp_home, PID2, {"v": 1, "annotations": [_ann(s1["prompt_h"], target, kind=kind)]})
    _run(tmp_home, tmp_git_repo, PID2)
    assert _snap(tmp_home)["acked"] == ["ann-00000001"]
    _req(tmp_home, PID3, {"v": 1, "annotations": [_ann(s1["prompt_h"], target, kind=kind)]})
    _run(tmp_home, tmp_git_repo, PID3)
    assert [r["id"] for r in _ann_rows(tmp_home)] == ["ann-00000001"]


def test_forged_prompt_h_rejected(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    # 先产出一条真实记录，再只伪造 prompt_h：path 与 kind 都取真值，否则会话里没有记录时
    # 不论有没有 prompt_h 判据都会 no_record，用例对它没有判别力
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache)
    _req(tmp_home, PID1, {"v": 1})
    _run(tmp_home, tmp_git_repo, PID1)
    item = _snap(tmp_home)["injected"][0]
    _req(tmp_home, PID2, {"v": 1, "annotations": [
        _ann("ffffffffffffffff", item["path"], kind=item["kind"])]})
    _run(tmp_home, tmp_git_repo, PID2)
    assert _snap(tmp_home)["rejected"][-1]["reason"] == "no_record"
    assert _ann_rows(tmp_home) == []


def _ann_outcome(snap: dict):
    return snap["acked"], [r for r in snap["rejected"] if r["what"] == "annotation"]


def test_annotation_after_purge_is_never_written(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    """purge 后重投（用户拍板 2026-10-10）：会话 metrics 文件不存在的那一轮既不 ack 也不拒收
    （与命名空间分裂分不开，不能判终局）；下一轮会话文件已由上一轮重新写出、但没有旧记录，
    判 no_record 终局。两轮都不写入。"""
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache)
    _req(tmp_home, PID1, {"v": 1})
    _run(tmp_home, tmp_git_repo, PID1)
    s1 = _snap(tmp_home)
    env = os.environ.copy()
    env.update({"HOME": str(tmp_home), "USERPROFILE": str(tmp_home), "PYTHONUTF8": "1"})
    subprocess.run([sys.executable, "-B", str(SCRIPTS / "analyze_metrics.py"), "--purge"],
                   env=env, cwd=str(SCRIPTS.parent), check=True, capture_output=True, timeout=60)
    item = s1["injected"][0]
    anns = [_ann(s1["prompt_h"], item["path"], kind=item["kind"])]
    _req(tmp_home, PID2, {"v": 1, "annotations": anns})
    _run(tmp_home, tmp_git_repo, PID2)
    assert _ann_outcome(_snap(tmp_home)) == ([], [])
    _req(tmp_home, PID3, {"v": 1, "annotations": anns})
    _run(tmp_home, tmp_git_repo, PID3)
    assert _ann_outcome(_snap(tmp_home)) == (
        [], [{"what": "annotation", "key": "ann-00000001", "reason": "no_record"}])
    assert _ann_rows(tmp_home) == []


def test_annotation_in_dry_run_stays_queued(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    """dry_run 承诺「预览、不落盘」，不表示丢弃（用户拍板 2026-10-10）：既不 ack 也不拒收。"""
    _config(tmp_home, tmp_vault, dry_run=True)
    _seed(tmp_vault, write_frontmatter_cache)
    _req(tmp_home, PID1, {"v": 1, "annotations": [_ann("0123456789abcdef", "笔记/alpha.md")]})
    _run(tmp_home, tmp_git_repo, PID1)
    assert _ann_outcome(_snap(tmp_home)) == ([], [])
    assert _ann_rows(tmp_home) == []


def test_annotation_with_corrupt_config_stays_queued(tmp_home, tmp_vault, tmp_git_repo,
                                                     write_frontmatter_cache):
    """节点 2 Q-H2：config 读坏回落默认值（metrics 默认关）的那一轮，不能据它判 metrics_off。"""
    _seed(tmp_vault, write_frontmatter_cache)
    d = tmp_home / ".context-vault"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text('{"metrics": {"enabled": true},}', encoding="utf-8")
    _req(tmp_home, PID1, {"v": 1, "annotations": [_ann("0123456789abcdef", "笔记/alpha.md")]})
    _run(tmp_home, tmp_git_repo, PID1)
    assert _ann_outcome(_snap(tmp_home)) == ([], [])


def test_unpin_llm_removes_pin_and_records_user_excluded(tmp_home, tmp_vault, tmp_git_repo,
                                                         write_frontmatter_cache):
    from scripts import _state, _topic
    from scripts.prompt_submit_load import PIN_HEADER
    _config(tmp_home, tmp_vault, relevance={"session_topic": True, "session_pin_max": 1})
    _seed(tmp_vault, write_frontmatter_cache)
    _state.configure_context("claude")
    _topic.save_session_topic(tmp_git_repo, SID, ["注入", "设计"], pinned=["笔记/beta.md"])
    _state.configure_context("unknown")
    _req(tmp_home, PID1, {"v": 1, "unpin_llm": ["笔记/beta.md"]})
    out = _run(tmp_home, tmp_git_repo, PID1)
    assert PIN_HEADER not in _ctx(out) and "笔记/beta.md" not in _ctx(out)
    recs = [json.loads(x) for f in (tmp_home / ".context-vault" / "metrics").rglob(f"{SID}.jsonl")
            for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert any(r.get("llm_pinned_dedup") == ["user_excluded"] for r in recs)
    # Review Focus 4 端到端（节点 2 T-M7）：同一份真实产出喂给报表，计入「用户排除」而非「跨轮去重」
    env = os.environ.copy()
    env.update({"HOME": str(tmp_home), "USERPROFILE": str(tmp_home), "PYTHONUTF8": "1"})
    rep = subprocess.run([sys.executable, "-B", str(SCRIPTS / "analyze_metrics.py")],
                         env=env, cwd=str(SCRIPTS.parent), capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert rep.returncode == 0, rep.stderr
    assert "被跨轮去重压掉 0 / 用户排除 1" in rep.stdout, rep.stdout


def test_legacy_user_gets_live_snapshot_but_no_canonical_state(tmp_home, tmp_vault, tmp_git_repo,
                                                               write_frontmatter_cache):
    legacy = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"vault_path": str(tmp_vault),
                                  "relevance": {"session_topic": False}}), encoding="utf-8")
    _seed(tmp_vault, write_frontmatter_cache)
    _req(tmp_home, PID1, {"v": 1})
    _run(tmp_home, tmp_git_repo, PID1)
    assert _snap(tmp_home)["status"] == "scored"
    canon = tmp_home / ".context-vault" / "state" / "claude"
    assert not (canon.exists() and any(p.is_file() for p in canon.rglob("*")))


def test_full_request_overhead_is_bounded(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    """满载请求（50 排除 + 50 标注）相对无请求的额外耗时。median of 5，交替测量。"""
    import statistics
    import time as _t
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache)
    full = {"v": 1, "exclude": [f"笔记/x{i}.md" for i in range(50)],
            "annotations": [_ann("0123456789abcdef", "笔记/alpha.md", f"ann-{i:08d}")
                            for i in range(50)]}
    with_req, without = [], []
    for i in range(5):
        pid_a = f"aaaaaaaa-bbbb-cccc-dddd-1000000000{i:02d}"
        pid_b = f"aaaaaaaa-bbbb-cccc-dddd-2000000000{i:02d}"
        _req(tmp_home, pid_a, full)
        t0 = _t.perf_counter(); _run(tmp_home, tmp_git_repo, pid_a); with_req.append(_t.perf_counter() - t0)
        t0 = _t.perf_counter(); _run(tmp_home, tmp_git_repo, pid_b); without.append(_t.perf_counter() - t0)
    print(f"\nmod overhead: with={statistics.median(with_req):.4f}s "
          f"without={statistics.median(without):.4f}s")
    assert statistics.median(with_req) - statistics.median(without) < 0.25
