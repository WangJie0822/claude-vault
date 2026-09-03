# -*- coding: utf-8 -*-
"""层 1 闸门：纯指代/应答型 prompt 直接不注入。

背景（2026-09-02 八路评审）：这批轮次实测占 5.6%，且当前 100% 靠单个通用词命中
（「继续执行」bigram 切成 {执行, 续执}，`续执` 命中 0 篇、`执行` 命中数百篇），
是召回质量最差的一批。

设计取舍：**整串匹配，不是前缀匹配**——「继续执行 F1，另外发现新问题」是有信息的轮次。
判据宁可漏拦不可误拦：漏拦只是维持现状，误拦会静默丢掉一次真实召回。

⚠️ 本文件的承重守卫是 test_gold_queries_never_gated —— 评审 R-C1 的教训是
「候选数/零注入率这类指标对相关性损失结构性失明」，故闸门的验收判据必须直接钉
「该给的有没有被拦掉」，而不是「拦了多少」。
"""
from __future__ import annotations

import copy

import pytest

from scripts._config_loader import DEFAULT_CONFIG
from scripts._decision import is_deictic_only
from tests.fixtures.gold_corpus import build_gold_corpus

CFG = DEFAULT_CONFIG


# ── 正向：应当被拦的形态 ────────────────────────────────────────────────
@pytest.mark.parametrize("prompt", [
    "继续执行",
    "继续",
    "接着",
    "然后",
    "下一步",
    "开始",
    "执行",
    "好的",
    "好",
    "嗯",
    "是的",
    "可以",
    "确认",
    "同意",
    "谢谢",
    "收到",
    "明白",
    "知道了",
    "ok",
    "OK",          # 大小写不敏感
    "Ok",
    "yes",
    "sure",
    "continue",
    "Continue",
    "go ahead",
    "do it",
    "proceed",
])
def test_pure_deictic_is_gated(prompt: str) -> None:
    assert is_deictic_only(prompt, CFG) is True, f"应被拦：{prompt!r}"


@pytest.mark.parametrize("prompt", [
    "继续执行。",
    "继续执行！",
    "好的，",
    "  ok  ",
    "继续~",
    "OK!!!",
    "明白了。",
])
def test_trailing_punctuation_and_space_tolerated(prompt: str) -> None:
    """尾随标点/空白不应让闸门失效——否则用户随手加个句号就绕过了。"""
    assert is_deictic_only(prompt, CFG) is True, f"应被拦：{prompt!r}"


# ── 反向：绝不能被拦的形态（误拦比漏拦危险得多）──────────────────────────
@pytest.mark.parametrize("prompt", [
    "继续执行 F1，另外发现新问题",        # 前缀匹配会误拦这条
    "继续执行上次那个 metrics 修复",
    "好的，那我们改用 tag_idf_factor",
    "ok，先修 fulltext 配额",
    "确认一下 cache 的 _version 要不要 bump",
    "执行完之后跑一下 gold recall",
    "然后呢？这个 df 口径怎么对齐",
    "continue with the rerank PoC",
    "是的，但 Jaccard 只有 0.03",
])
def test_prompt_with_substance_is_not_gated(prompt: str) -> None:
    assert is_deictic_only(prompt, CFG) is False, f"不该被拦：{prompt!r}"


def test_empty_prompt_is_not_gated() -> None:
    """空 prompt 交给既有的 too_few_keywords 闸门处理，本闸门不越权。

    两条闸门各管各的：空串没有「指代」语义，把它算进本闸门会让 gate 归因失真
    （报表上看是「被指代闸门拦的」，实际是没词）。
    """
    assert is_deictic_only("", CFG) is False
    assert is_deictic_only("   ", CFG) is False
    assert is_deictic_only("\n\t ", CFG) is False


# ── 承重守卫：不得误伤 gold 语料的任何一条真实查询 ──────────────────────
def test_gold_queries_never_gated() -> None:
    """23 条 gold query 一条都不能被本闸门拦下。

    这是层 1 的 recall 守卫：闸门唯一可能损害召回的方式就是拦掉本该召回的 query，
    故直接断言「一条都没拦」比重跑 recall 更精确（重跑 recall 走的是 decide_injection，
    根本不经过本闸门，那条路测不到这里）。
    """
    _corpus, queries = build_gold_corpus()
    gated = [q.prompt for q in queries if is_deictic_only(q.prompt, CFG)]
    assert gated == [], f"闸门误伤了 gold query：{gated}"


# ── 配置面 ──────────────────────────────────────────────────────────────
def test_gate_can_be_disabled_by_config() -> None:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["relevance"]["deictic_gate"] = False
    assert is_deictic_only("继续执行", cfg) is False


def test_word_list_is_configurable() -> None:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["relevance"]["deictic_words"] = ["蛤"]
    assert is_deictic_only("蛤", cfg) is True
    assert is_deictic_only("继续执行", cfg) is False, "自定义词表应完全替换默认表"


def test_missing_config_keys_fall_back_to_default() -> None:
    """config 缺键不得抛异常——hook 必须 fail-open。"""
    assert is_deictic_only("继续执行", {}) is True
    assert is_deictic_only("继续执行", {"relevance": {}}) is True


@pytest.mark.parametrize("bad", [None, 123, [], {}])
def test_non_string_prompt_never_raises(bad) -> None:
    """畸形输入不得抛异常（fail-open 不变量）。"""
    assert is_deictic_only(bad, CFG) is False


# ---------------------------------------------------------------------------
# 端到端接线：闸门必须真的让 main() 早退，并落一条 gate=deictic_only 的记录
# ---------------------------------------------------------------------------
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prompt_submit_load.py"


def _run(cwd: Path, prompt: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    payload = {"cwd": str(cwd), "prompt": prompt,
               "session_id": "sess-D", "prompt_id": "pid-D"}
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", env=env, timeout=15,
    )


def _read_records(tmp_home: Path) -> list[dict]:
    md = tmp_home / ".claude" / "vault-loader-metrics"
    out = []
    for f in sorted(md.rglob("*.jsonl")):
        if f.parent.name == md.name:
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _setup(tmp_home: Path, tmp_vault: Path, write_frontmatter_cache) -> None:
    write_frontmatter_cache({
        "技术笔记/hook.md": {
            "tags": ["hook", "skill"],
            "category": "技术笔记",
            "summary": "SessionStart hook 实现",
            "mtime": 1900000000,
        }
    })
    (tmp_vault / "技术笔记").mkdir()
    (tmp_vault / "技术笔记" / "hook.md").write_text("# hook", encoding="utf-8")
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "dry_run": False,
        "vault_path": str(tmp_vault),
        "metrics": {"enabled": True, "near_miss_k": 10},
    }), encoding="utf-8")


def test_deictic_prompt_exits_early_end_to_end(tmp_home: Path, tmp_vault: Path,
                                               write_frontmatter_cache):
    """「继续执行」必须走到早退：不注入任何内容，且落 gate=deictic_only。"""
    cwd = tmp_home.parent / "proj-deictic"
    cwd.mkdir()
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    r = _run(cwd, "继续执行")
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout) if r.stdout.strip() else {}
    ctx = (payload.get("hookSpecificOutput") or {}).get("additionalContext", "")
    assert not ctx, f"指代型 prompt 不该注入，却注入了 {len(ctx)} 字符"
    gates = [x for x in _read_records(tmp_home) if x.get("gate")]
    assert gates, "没有产生闸门早退记录"
    assert gates[-1]["gate"] == "deictic_only", gates[-1]
    assert set(gates[-1]) == {"_schema", "ts", "session", "prompt_id", "gate"}, (
        f"极简 gate 记录漂入新键：{sorted(set(gates[-1]) - {'_schema', 'ts', 'session', 'prompt_id', 'gate'})}")


def test_deictic_prefix_with_substance_still_injects(tmp_home: Path, tmp_vault: Path,
                                                     write_frontmatter_cache):
    """阳性对照：以「继续执行」开头但带实质内容的 prompt 必须照常注入。

    没有这条，上一条用例在「闸门拦下一切」的实现下也会绿 —— 那是最坏的失效方向。
    """
    cwd = tmp_home.parent / "proj-substance"
    cwd.mkdir()
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    r = _run(cwd, "继续执行 hook 的 SessionStart 实现")
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout) if r.stdout.strip() else {}
    ctx = (payload.get("hookSpecificOutput") or {}).get("additionalContext", "")
    assert ctx, "带实质内容的 prompt 被误拦，没有产生注入"
    gates = [x for x in _read_records(tmp_home) if x.get("gate") == "deictic_only"]
    assert not gates, f"带实质内容的 prompt 不该落 deictic_only：{gates}"
