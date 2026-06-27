"""prompt_submit_load 集成测试。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.prompt_submit_load import build_injection_text_ups, build_summary_ups
from scripts._frontmatter_reader import Entry

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prompt_submit_load.py"


def _run(cwd: Path, prompt: str, env_extra: dict | None = None,
         input_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # 子进程强制 UTF-8（镜像生产；Windows 默认 GBK 会令 hook 输出 emoji/中文失败）
    env.setdefault("PYTHONUTF8", "1")
    if env_extra:
        env.update(env_extra)
    payload = {"cwd": str(cwd), "prompt": prompt}
    if input_extra:
        payload.update(input_extra)   # 注入额外 hook-input 字段（如 promptSource）
    hook_input = json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=hook_input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=5,
    )


def _parse(r):
    out = r.stdout.strip()
    return json.loads(out) if out else None


# ---------------------------------------------------------------------------
# golden 等价守护
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# build_summary_ups 格式守护
# ---------------------------------------------------------------------------

def test_build_summary_ups_list_format():
    # B：高置信需 ≥2 个不同关键词佐证，故 entry 带 hook+skill 两个 tag（hook 命中 tag+summary、
    # skill 命中 tag → dist=2）；否则单关键词 topical=6 也只标中置信。
    items = [(8.0, 6.0, Entry(path="技术笔记/hook.md", tags=("hook", "skill"),
                              summary="hook 实现", mtime=1900000000))]
    injection_text = "x" * 1500
    rel_cfg = {"confidence_bands": {"high": 6}, "short_summary_chars": 20}
    out = build_summary_ups(
        items, {"hook", "skill"}, None, injection_text,
        {"verbosity": "list", "show_size": True}, rel_cfg,
    )
    assert out.startswith("📚 vault-loader · 提问注入 · 1 笔记 · 关键词[hook, skill] · ~1.5k 字")
    assert "- hook  [高置信]" in out


def test_build_summary_ups_compact_format():
    items = [(6.0, 4.0, Entry(path="技术笔记/hook.md", summary="hook 实现", mtime=1900000000))]
    injection_text = "y" * 300
    rel_cfg = {"confidence_bands": {"high": 6}, "short_summary_chars": 20}
    out = build_summary_ups(
        items, {"hook"}, None, injection_text,
        {"verbosity": "compact", "show_size": True}, rel_cfg,
    )
    assert out == "📚 vault-loader(提问): 1笔记[hook] 关键词[hook] · ~300 字 · /vault 展开"


def test_build_injection_text_ups_list_golden() -> None:
    """清单模式：候选清单 + 置信度档 + 命中词 + 自选指令。"""
    ups_cfg = {"max_notes": 3, "fulltext_max_bytes": 8192}
    rel_cfg = {"confidence_bands": {"high": 6}, "fulltext_topical_threshold": 6,
               "short_summary_chars": 20}
    long_summary = "SessionStart hook 的注入机制设计与实现说明文档"  # ≥ short_summary_chars
    scored_mid = [(6.0, 4.0, Entry(path="技术笔记/hook.md", summary=long_summary, mtime=1900000000))]
    text, paths, ft = build_injection_text_ups(
        scored_mid, "hook, skill", {"hook"}, ups_cfg, rel_cfg, vault_path=Path("/nonexistent"))
    assert ft is None
    assert "📚 vault-loader 候选（按本轮提问关键词粗筛：hook, skill）" in text
    assert f"- [[技术笔记/hook.md]]（中置信，命中：hook）— {long_summary}" in text
    assert "流程词" in text   # 自选指令
    assert paths == ["技术笔记/hook.md"]


def test_candidate_title_falls_back_for_empty_summary() -> None:
    from scripts.prompt_submit_load import _candidate_title
    e = Entry(path="技术笔记/无摘要.md", summary="")
    out = _candidate_title(e, 20)
    assert "无摘要" in out and "summary 缺失" in out


def test_fulltext_picks_topical_max_not_total_top() -> None:
    """arch F1 回归：强话题(topical=6)但 total 较低的条目应触发全文，
    而非 total 排序首位的弱话题(topical=4)条目。"""
    ups_cfg = {"max_notes": 3, "fulltext_max_bytes": 8192}
    rel_cfg = {"confidence_bands": {"high": 6}, "fulltext_topical_threshold": 6,
               "short_summary_chars": 20}
    b = Entry(path="技术笔记/weak.md", summary="弱话题但项目相关的笔记摘要内容", mtime=1900000000)
    # B：全文候选需 ≥2 个不同关键词佐证，故 A 带 alpha+beta 两个命中 tag（dist=2）。
    a = Entry(path="技术笔记/strong.md", tags=("alpha", "beta"),
              summary="强话题命中的笔记摘要内容说明", mtime=1900000000)
    scored = [(8.0, 4.0, b), (6.0, 6.0, a)]   # 按 total 降序（B 在前），但 A 的 topical 更强
    text, paths, ft = build_injection_text_ups(
        scored, "kw", {"alpha", "beta"}, ups_cfg, rel_cfg, vault_path=Path("/nonexistent"))
    assert ft == "技术笔记/strong.md"           # 全文取 topical 最强的 A，而非 total 首位 B
    assert paths[0] == "技术笔记/strong.md"


# ---------------------------------------------------------------------------
# 集成测试
# ---------------------------------------------------------------------------

def test_short_prompt_silent(tmp_home: Path, tmp_vault: Path) -> None:
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault)}))

    r = _run(Path("/tmp"), "hi")
    assert r.stdout.strip() == ""


def test_list_mode_typical(tmp_home: Path, tmp_vault: Path, write_frontmatter_cache) -> None:
    write_frontmatter_cache({
        "技术笔记/hook.md": {
            "tags": ["hook", "skill"],
            "category": "技术笔记",
            "summary": "SessionStart hook 实现",
            "mtime": 1900000000,
        }
    })
    (tmp_vault / "技术笔记").mkdir()
    (tmp_vault / "技术笔记" / "hook.md").write_text("# hook")

    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault)}))

    r = _run(Path("/tmp"), "please explain the SessionStart hook implementation")
    d = _parse(r); assert d is not None
    assert "📚" in d["systemMessage"]
    assert "hook.md" in d["hookSpecificOutput"]["additionalContext"]


def test_fulltext_mode_triggered(tmp_home: Path, tmp_vault: Path,
                                   write_frontmatter_cache) -> None:
    """Top 1 score ≥ 10 时注入全文。"""
    note_dir = tmp_vault / "技术笔记"
    note_dir.mkdir()
    (note_dir / "vault-loader.md").write_text("# vault-loader\n\n这是全文内容", encoding="utf-8")

    write_frontmatter_cache({
        "技术笔记/vault-loader.md": {
            "tags": ["hook", "vault-loader", "skill", "spec", "automated"],
            "category": "技术笔记",
            "summary": "vault-loader hook spec",
            "mtime": 1900000000,
        }
    })

    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault)}))

    # prompt 命中多个 tag → 触发 prompt_tag_hit + prompt_summary_hit
    prompt = "vault-loader hook skill spec automated"
    r = _run(Path("/tmp"), prompt)

    d = _parse(r); assert d is not None
    ac = d["hookSpecificOutput"]["additionalContext"]
    assert "这是全文内容" in ac or "vault-loader" in ac
    assert "📚" in d["systemMessage"]


def test_dedup_via_state(tmp_home: Path, tmp_vault: Path, write_frontmatter_cache) -> None:
    """已在 state 的弱候选 path（topical 未升到全文阈值）不应再次注入。"""
    write_frontmatter_cache({
        "技术笔记/hook.md": {
            "tags": ["hook", "skill"],
            "summary": "某模块实现说明",   # 不含 hook/skill 英文 → 本轮仅 tag 命中 topical=4<6 不升级
            "mtime": 1900000000,
        }
    })
    (tmp_vault / "技术笔记").mkdir()
    (tmp_vault / "技术笔记" / "hook.md").write_text("# hook")

    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault)}))

    # 预填 state
    from scripts._state import save_injected
    save_injected(Path("/tmp"), ["技术笔记/hook.md"])

    r = _run(Path("/tmp"), "explain hook implementation skill")
    # 应静默或不含 hook.md
    d = _parse(r)
    assert d is None or "hook.md" not in d["hookSpecificOutput"]["additionalContext"]


def test_disable_via_env(tmp_home: Path) -> None:
    r = _run(Path("/tmp"), "explain hook implementation skill",
             env_extra={"VAULT_LOADER_DISABLE": "1"})
    assert r.stdout.strip() == ""


# ---------------------------------------------------------------------------
# 根因场景回归（相关性优化）
# ---------------------------------------------------------------------------

def test_root_cause_slash_noise_silent(tmp_home: Path, tmp_vault: Path,
                                       write_frontmatter_cache) -> None:
    """根因复现：/superpowers:brainstorming + 无 vault 沉淀的话题词 → 静默。
    剥 slash 命令名后只剩 当前提示浮层…/bugid，均不命中该 superpowers 笔记 → topical=0 被挡。"""
    write_frontmatter_cache({
        "Claude Code/某 superpowers 实战.md": {
            "tags": ["claude-code", "skill", "superpowers"],
            "summary": "某 superpowers 全链路实战",
            "mtime": 1900000000,
        }
    })
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault)}))

    r = _run(Path("/tmp"), "/superpowers:brainstorming 当前提示浮层高度会折叠bugid")
    # 核心不变量：不注入无关 superpowers 笔记（无 additionalContext）。
    # 该 prompt 有 2 关键词(bugid+中文串)、全 topical 失配 → 触发点2 出兜底提示（用户已批准），
    # 但不注入任何笔记。
    d = _parse(r)
    assert d is None or "hookSpecificOutput" not in d


def test_topical_match_injects_with_confidence(tmp_home: Path, tmp_vault: Path,
                                               write_frontmatter_cache) -> None:
    """正样本：话题词命中 tag（topical=4，list 模式）→ 过闸 + 带中置信度档。"""
    write_frontmatter_cache({
        "技术笔记/hook.md": {
            "tags": ["hook", "skill"],
            "summary": "某模块的设计说明文档与背景介绍",   # 不含 hook/skill → 仅 tag 命中
            "mtime": 1900000000,
        }
    })
    (tmp_vault / "技术笔记").mkdir()
    (tmp_vault / "技术笔记" / "hook.md").write_text("# hook", encoding="utf-8")
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault)}))

    r = _run(Path("/tmp"), "explain the hook skill design")
    d = _parse(r); assert d is not None
    assert "中置信" in d["hookSpecificOutput"]["additionalContext"]


def test_control_char_keyword_sanitized(tmp_home: Path, tmp_vault: Path,
                                        write_frontmatter_cache) -> None:
    """token 正则本就排除控制字符；端到端确认 systemMessage 无裸控制字节。"""
    write_frontmatter_cache({
        "技术笔记/hook.md": {"tags": ["hook", "skill"],
                            "summary": "某模块的设计说明文档与背景介绍", "mtime": 1900000000}
    })
    (tmp_vault / "技术笔记").mkdir()
    (tmp_vault / "技术笔记" / "hook.md").write_text("# hook", encoding="utf-8")
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault)}))

    r = _run(Path("/tmp"), "explain hook \x1b[31m skill design")
    d = _parse(r)
    if d and d.get("systemMessage"):
        assert "\x1b" not in d["systemMessage"]   # 无裸 ESC


# ---------------------------------------------------------------------------
# 全文升级（弱候选→强命中）
# ---------------------------------------------------------------------------

def _write_cfg(tmp_home: Path, tmp_vault: Path, relevance: dict | None = None) -> None:
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    body = {"dry_run": False, "vault_path": str(tmp_vault)}
    if relevance is not None:
        body["relevance"] = relevance
    cfg.write_text(json.dumps(body))


def test_fulltext_upgrade_from_candidate(tmp_home: Path, tmp_vault: Path,
                                         write_frontmatter_cache) -> None:
    """曾以弱候选注入的篇，本轮升到 topical=6 → 重注为全文 + 入 fulltext_paths。"""
    from scripts._state import save_injected, load_fulltext_injected
    write_frontmatter_cache({
        "技术笔记/up.md": {"tags": ["hook", "skill"],
                          "summary": "hook 的设计实现说明文档详述", "mtime": 1900000000}
    })
    (tmp_vault / "技术笔记").mkdir()
    (tmp_vault / "技术笔记" / "up.md").write_text("# up\n\n这是升级全文内容", encoding="utf-8")
    _write_cfg(tmp_home, tmp_vault)
    # 预填：该篇曾以弱候选注入（在 paths、不在 fulltext_paths）
    save_injected(Path("/tmp"), ["技术笔记/up.md"])

    # 本轮 hook 命中 tag(+4) + summary(+2) = topical 6 → 升级候选 → 全文
    r = _run(Path("/tmp"), "explain the hook skill implementation")
    d = _parse(r); assert d is not None
    ac = d["hookSpecificOutput"]["additionalContext"]
    assert "这是升级全文内容" in ac          # 重注为全文
    assert "强命中" in ac
    assert "技术笔记/up.md" in load_fulltext_injected(Path("/tmp"), 24)


def test_upgrade_candidate_not_primary_stays_visible(tmp_home: Path, tmp_vault: Path,
                                                     write_frontmatter_cache) -> None:
    """reverse High#1 固化：两篇升级候选(topical=6)竞争，仅 total 高者升全文主候选；
    total 低的升级候选仍在 rest 清单可见、不入 fulltext_paths（保留升级机会，不凭空消失）。"""
    from scripts._state import save_injected, load_fulltext_injected
    write_frontmatter_cache({
        "技术笔记/a.md": {"tags": ["hook", "skill"],
                         "summary": "hook 的实现说明详述与背景介绍文档资料",
                         "mtime": 1900000000},   # 未来 mtime → +1 → total 更高 → 夺主候选
        "技术笔记/b.md": {"tags": ["hook", "skill"],
                         "summary": "hook 的另一实现说明详述与背景资料",
                         "mtime": 1262304000},   # 2010 → mtime 加成 0 → total 低
    })
    (tmp_vault / "技术笔记").mkdir()
    (tmp_vault / "技术笔记" / "a.md").write_text("# a\n\nA 全文内容", encoding="utf-8")
    (tmp_vault / "技术笔记" / "b.md").write_text("# b\n\nB 全文内容", encoding="utf-8")
    _write_cfg(tmp_home, tmp_vault)
    # 两篇都曾以弱候选注入
    save_injected(Path("/tmp"), ["技术笔记/a.md", "技术笔记/b.md"])

    r = _run(Path("/tmp"), "explain the hook skill implementation")  # 两篇均 topical=6
    d = _parse(r); assert d is not None
    ac = d["hookSpecificOutput"]["additionalContext"]
    assert "A 全文内容" in ac                         # a 升全文主候选
    assert "[[技术笔记/b.md]]" in ac                  # b 仍在候选清单可见（非主候选不消失）
    ft = load_fulltext_injected(Path("/tmp"), 24)
    assert "技术笔记/a.md" in ft                       # a 入 fulltext_paths
    assert "技术笔记/b.md" not in ft                   # b 未入 → 保留下轮升级机会


def test_fulltext_path_never_reinjected(tmp_home: Path, tmp_vault: Path,
                                        write_frontmatter_cache) -> None:
    """已在 fulltext_paths 的篇 → 恒跳过，不再重注。"""
    from scripts._state import save_injected
    write_frontmatter_cache({
        "技术笔记/up.md": {"tags": ["hook", "skill"],
                          "summary": "hook 的设计实现说明文档详述", "mtime": 1900000000}
    })
    (tmp_vault / "技术笔记").mkdir()
    (tmp_vault / "技术笔记" / "up.md").write_text("# up\n\n全文", encoding="utf-8")
    _write_cfg(tmp_home, tmp_vault)
    save_injected(Path("/tmp"), ["技术笔记/up.md"], fulltext_paths=["技术笔记/up.md"])

    r = _run(Path("/tmp"), "explain the hook skill implementation")
    # 唯一篇已在 fulltext_paths → scored 空 → 静默（且因仍相关，不出兜底）
    d = _parse(r)
    assert d is None or "up.md" not in d.get("hookSpecificOutput", {}).get("additionalContext", "")


def test_low_candidate_not_reinjected_silent(tmp_home: Path, tmp_vault: Path,
                                             write_frontmatter_cache) -> None:
    """弱候选本轮仍只 topical=4(<ft 阈值6) → 不重复展示；因仍相关不出兜底（静默）。"""
    from scripts._state import save_injected
    write_frontmatter_cache({
        "技术笔记/up.md": {"tags": ["hook", "skill"],
                          "summary": "某模块设计说明", "mtime": 1900000000}  # summary 无英文命中
    })
    (tmp_vault / "技术笔记").mkdir()
    (tmp_vault / "技术笔记" / "up.md").write_text("# up", encoding="utf-8")
    _write_cfg(tmp_home, tmp_vault)
    save_injected(Path("/tmp"), ["技术笔记/up.md"])  # 弱候选

    r = _run(Path("/tmp"), "explain the hook skill design")  # 仅 tag 命中 → topical=4
    assert r.stdout.strip() == ""    # 不重注、不兜底（相关篇已展示过）


# ---------------------------------------------------------------------------
# 兜底提示（仅触发点2：关键词足够但 topical 全失配）
# ---------------------------------------------------------------------------

def test_fallback_hint_on_all_topical_filtered(tmp_home: Path, tmp_vault: Path,
                                               write_frontmatter_cache) -> None:
    """关键词≥min 但无任何篇 topical 命中 → 一行用户可见兜底（不进 additionalContext）。"""
    write_frontmatter_cache({
        "技术笔记/other.md": {"tags": ["xyz"], "summary": "毫不相关的内容", "mtime": 1900000000}
    })
    _write_cfg(tmp_home, tmp_vault)
    r = _run(Path("/tmp"), "explain the hook skill design")  # 无篇命中 → topical 全 0
    d = _parse(r); assert d is not None
    assert "未匹配到强相关" in d["systemMessage"]
    assert "/vault" in d["systemMessage"]
    # 兜底只走 systemMessage，不进 additionalContext（emit(None,...) 省略 hookSpecificOutput）
    assert "hookSpecificOutput" not in d


def test_no_fallback_on_keyword_count_gate(tmp_home: Path, tmp_vault: Path,
                                           write_frontmatter_cache) -> None:
    """触发点1（关键词数<min，如中文短追问）→ 静默，不出兜底（防刷屏）。"""
    write_frontmatter_cache({
        "技术笔记/other.md": {"tags": ["xyz"], "summary": "无关", "mtime": 1900000000}
    })
    _write_cfg(tmp_home, tmp_vault)
    r = _run(Path("/tmp"), "改一下")  # 中文整串=1 token < min_keyword_count=2
    assert r.stdout.strip() == ""    # 完全静默，无兜底


def test_fallback_hint_disabled(tmp_home: Path, tmp_vault: Path,
                                write_frontmatter_cache) -> None:
    """fallback_hint=false → 全失配也静默。"""
    write_frontmatter_cache({
        "技术笔记/other.md": {"tags": ["xyz"], "summary": "无关", "mtime": 1900000000}
    })
    _write_cfg(tmp_home, tmp_vault, relevance={"fallback_hint": False})
    r = _run(Path("/tmp"), "explain the hook skill design")
    assert r.stdout.strip() == ""


# ---------------------------------------------------------------------------
# 非用户输入拦截（task-notification / promptSource=system）
# ---------------------------------------------------------------------------

TASK_NOTIFICATION = (
    "<task-notification>\n<task-id>b14oqi6e7</task-id>\n"
    "<tool-use-id>toolu_01JLfoPJALhP6zzSuMD6WJtL</tool-use-id>\n"
    "<output-file>D:/Temp/claude/D--Work-Workspace-ProjectA/x/b14oqi6e7.output</output-file>\n"
    "<status>completed</status>\n<summary>Background command \"hook skill 设计实现\" completed</summary>\n"
    "</task-notification>"
)


def _cache_hook_note(write_frontmatter_cache, tmp_vault):
    write_frontmatter_cache({
        "技术笔记/hook.md": {"tags": ["hook", "skill"],
                            "summary": "hook 的设计实现说明详述文档", "mtime": 1900000000}
    })
    (tmp_vault / "技术笔记").mkdir()
    (tmp_vault / "技术笔记" / "hook.md").write_text("# hook\n\n全文", encoding="utf-8")


def test_task_notification_wrapper_skipped(tmp_home: Path, tmp_vault: Path,
                                           write_frontmatter_cache) -> None:
    """<task-notification> 包裹的系统注入 prompt → 跳过（不依赖未文档化字段，文本闸保底）。
    内层含 hook/skill（会命中 hook.md），仍静默 → 证明是包裹导致 skip 而非无匹配。"""
    _cache_hook_note(write_frontmatter_cache, tmp_vault)
    _write_cfg(tmp_home, tmp_vault)
    r = _run(Path("/tmp"), TASK_NOTIFICATION)
    assert r.stdout.strip() == ""


def test_prompt_source_system_skipped(tmp_home: Path, tmp_vault: Path,
                                      write_frontmatter_cache) -> None:
    """promptSource=system（非 typed）→ 跳过，即便 prompt 文本会命中。"""
    _cache_hook_note(write_frontmatter_cache, tmp_vault)
    _write_cfg(tmp_home, tmp_vault)
    r = _run(Path("/tmp"), "explain the hook skill implementation",
             input_extra={"promptSource": "system"})
    assert r.stdout.strip() == ""


def test_prompt_source_snake_case_also_honored(tmp_home: Path, tmp_vault: Path,
                                               write_frontmatter_cache) -> None:
    """snake_case prompt_source=system 同样被识别（兼容两种命名）。"""
    _cache_hook_note(write_frontmatter_cache, tmp_vault)
    _write_cfg(tmp_home, tmp_vault)
    r = _run(Path("/tmp"), "explain the hook skill implementation",
             input_extra={"prompt_source": "system"})
    assert r.stdout.strip() == ""


def test_prompt_source_typed_processed(tmp_home: Path, tmp_vault: Path,
                                       write_frontmatter_cache) -> None:
    """promptSource=typed（真实手输）→ 正常处理注入。"""
    _cache_hook_note(write_frontmatter_cache, tmp_vault)
    _write_cfg(tmp_home, tmp_vault)
    r = _run(Path("/tmp"), "explain the hook skill implementation",
             input_extra={"promptSource": "typed"})
    d = _parse(r); assert d is not None
    assert "hook.md" in d["hookSpecificOutput"]["additionalContext"]


def test_no_prompt_source_field_processed(tmp_home: Path, tmp_vault: Path,
                                          write_frontmatter_cache) -> None:
    """无 promptSource 字段（字段未文档化、可能不下发）→ 正常处理（向后兼容）。"""
    _cache_hook_note(write_frontmatter_cache, tmp_vault)
    _write_cfg(tmp_home, tmp_vault)
    r = _run(Path("/tmp"), "explain the hook skill implementation")
    d = _parse(r); assert d is not None
    assert "hook.md" in d["hookSpecificOutput"]["additionalContext"]


def test_prompt_source_queued_processed(tmp_home: Path, tmp_vault: Path,
                                        write_frontmatter_cache) -> None:
    """promptSource=queued 是真实用户排队消息（实证 transcript）→ 必须正常处理，不被黑名单误杀。
    钉死「黑名单仅拦 system」而非「≠typed 白名单」语义（节点2 评审）。"""
    _cache_hook_note(write_frontmatter_cache, tmp_vault)
    _write_cfg(tmp_home, tmp_vault)
    r = _run(Path("/tmp"), "explain the hook skill implementation",
             input_extra={"promptSource": "queued"})
    d = _parse(r); assert d is not None
    assert "hook.md" in d["hookSpecificOutput"]["additionalContext"]


def test_prompt_source_empty_string_processed(tmp_home: Path, tmp_vault: Path,
                                              write_frontmatter_cache) -> None:
    """promptSource 空串（未知来源）→ 按用户输入处理，不误杀（节点2 Low：or 短路边界）。"""
    _cache_hook_note(write_frontmatter_cache, tmp_vault)
    _write_cfg(tmp_home, tmp_vault)
    r = _run(Path("/tmp"), "explain the hook skill implementation",
             input_extra={"promptSource": ""})
    d = _parse(r); assert d is not None
    assert "hook.md" in d["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# T-H1：keyword-only 命中端到端行为测试
# ---------------------------------------------------------------------------

def test_keyword_only_note_injected_as_candidate_not_fulltext(
        tmp_home, tmp_vault, write_frontmatter_cache):
    # T-H1：keyword-only 命中（topical=3 < min_topical=4）应经 hook 真注入候选清单、不触发全文
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault)}),
                   encoding="utf-8")
    write_frontmatter_cache({
        "技术笔记/kw.md": {"tags": ["misc"], "summary": "一段与查询无关的摘要内容",
                          "keywords": ["扩展词召回", "相关性打分"], "mtime": 1900000000},
    })
    r = _run(tmp_vault, "扩展词召回 相关性打分 怎么实现")
    data = _parse(r)
    assert data is not None, r.stderr
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "vault-loader 候选" in ctx            # 清单模式（非全文）
    assert "技术笔记/kw.md" in ctx                # keyword-only 笔记进了候选
    assert "自动加载全文" not in ctx              # 未触发自动全文


def test_skip_non_user_disabled_processes_notification(tmp_home: Path, tmp_vault: Path,
                                                       write_frontmatter_cache) -> None:
    """skip_non_user_prompts=false → 即便 task-notification 也处理（可关闭的逃生阀）。"""
    _cache_hook_note(write_frontmatter_cache, tmp_vault)
    _write_cfg(tmp_home, tmp_vault, relevance={"skip_non_user_prompts": False})
    r = _run(Path("/tmp"), TASK_NOTIFICATION)
    # 关闭拦截后会处理（内层 hook/skill 命中）→ 有输出
    assert r.stdout.strip() != ""


# ---------------------------------------------------------------------------
# B 纵深防御：最强档（自动全文 / 高置信）需 ≥2 个不同关键词佐证
# ---------------------------------------------------------------------------

def test_b_single_keyword_no_fulltext() -> None:
    """单个关键词刷满 topical=6（同时命中 tag+summary）不触发自动全文，降级为清单中置信。"""
    ups_cfg = {"max_notes": 3, "fulltext_max_bytes": 8192}
    rel_cfg = {"confidence_bands": {"high": 6}, "fulltext_topical_threshold": 6,
               "short_summary_chars": 20}
    e = Entry(path="技术笔记/single.md", tags=("hook",),
              summary="hook 的设计实现说明详述文档资料", mtime=1900000000)
    scored = [(6.0, 6.0, e)]
    text, paths, ft = build_injection_text_ups(
        scored, "hook", {"hook"}, ups_cfg, rel_cfg, vault_path=Path("/nonexistent"))
    assert ft is None                              # 单关键词不触发全文
    assert "📚 vault-loader 候选" in text           # 走清单模式
    assert "中置信" in text                          # 降级为中置信


def test_b_two_distinct_keywords_trigger_fulltext(tmp_home: Path, tmp_vault: Path,
                                                  write_frontmatter_cache) -> None:
    """≥2 个不同关键词命中（hook 命中 tag+summary、skill 命中 tag）→ 仍触发全文。"""
    note_dir = tmp_vault / "技术笔记"
    note_dir.mkdir()
    (note_dir / "two.md").write_text("# two\n\n这是双词全文内容", encoding="utf-8")
    write_frontmatter_cache({
        "技术笔记/two.md": {"tags": ["hook", "skill"],
                           "summary": "hook 的设计实现说明详述文档", "mtime": 1900000000}
    })
    _write_cfg(tmp_home, tmp_vault)
    # hook 命中 tag+summary、skill 命中 tag → 2 个不同关键词 → topical 6 且 dist≥2
    r = _run(Path("/tmp"), "explain the hook skill implementation")
    d = _parse(r); assert d is not None
    ac = d["hookSpecificOutput"]["additionalContext"]
    assert "这是双词全文内容" in ac
    assert "强命中" in ac


def test_b_summary_single_keyword_mid_band() -> None:
    """build_summary_ups：单关键词刷满 topical=6 → 标签降为中置信。"""
    items = [(6.0, 6.0, Entry(path="技术笔记/single.md", tags=("hook",),
                              summary="hook 的设计实现详述", mtime=1900000000))]
    rel_cfg = {"confidence_bands": {"high": 6}, "short_summary_chars": 20}
    out = build_summary_ups(
        items, {"hook"}, None, "x" * 100,
        {"verbosity": "list", "show_size": True}, rel_cfg,
    )
    assert "- single  [中置信]" in out
