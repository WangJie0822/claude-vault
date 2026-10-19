"""`private` tag 的外发排除：写端 → cache → 读端 → 排除判定 → 外发 prompt 端到端守卫（2.4.0 full-review S-H1）。

评审 PoC 复现：七种带 `private` 的写法里只有 `tags: [private]` 被排除，其余几种的文件名与摘要都照样进了
精排外发 prompt。根因分在三处——写端回落解析器（未装 PyYAML 时）不认 YAML 块序列、读端把非 list 的 tags
清空且截断先于判定、排除判定只做精确匹配——任何一处单独修好都挡不住其余写法，所以这里从真实写端跑起：

1. 子进程 A：`summarize-session/scripts/rebuild_index.main()` 对临时 vault 建 cache。**强制走回落解析器**
   （`_HAS_YAML = False`）：本用例要钉住的正是「默认依赖、不装 PyYAML」那条路径；装了 PyYAML 的机器上
   `tags: #private` 是 YAML 注释、本就不算 tag，结论不同。块序列在两种解析器下的一致性由
   `skills/summarize-session/tests/test_rebuild_index_block_seq.py` 对拍。
2. 子进程 B：vault-loader 的 `load_cache_status` → `exclusion_set` / `build_fine_input` →
   `build_pin_prompt` / `build_supp_prompt`，与精排子进程（`_topic` 里的那两次调用）同参；排除 tag 取
   `DEFAULT_CONFIG` 的默认值（`fine_exclude_tags` 默认 `["private"]`）。

两个 skill 不互相导入，且仓库根也有 `scripts` 包（与 vault-loader 的顶层包名相撞），所以分两个子进程跑，
先例见同目录 `test_worklog_parity.py`。HOME / USERPROFILE 指到 tmp，不碰真实 HOME 与 vault。

外发通路两条都测：候选（摘要层 + 目录层）与「已在上下文」（已注入笔记的文件名）。对照组是不带 private 的
笔记——它必须仍在 prompt 里，否则「什么都不外发」也会让断言全绿。

frontmatter 读取窗口（D-M1）的两种形态也从这里跑：frontmatter 长于 2000 字符的（写端须读到闭合行），与
读满 64 Ki 字符仍未闭合的（写端记 `fm_truncated`、读端判 tag 不全）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SS = ROOT / "skills" / "summarize-session" / "scripts"
VL = ROOT / "skills" / "vault-loader"

# 文件名带 PRIV_ / 摘要带 SECRET- 的笔记都带 private（各种写法），一个都不得外发
_FILLER = "".join(f"  - filler{i}\n" for i in range(39))
NOTES = {
    "PRIV_block": "tags:\n  - private\n",                       # 块序列
    "PRIV_block_flush": "tags:\n- diary\n- private\n",          # 块序列不缩进（合法 YAML）
    "PRIV_block_quoted": "tags:\n  - \"Private\"\n",
    "PRIV_block_nested": "tags:\n  - private/journal\n",
    "PRIV_scalar": "tags: private\n",
    "PRIV_comma": "tags: diary, private\n",
    "PRIV_space": "tags: diary private\n",
    "PRIV_flow": "tags: [private]\n",
    "PRIV_hash_quoted": "tags: \"#private\"\n",
    "PRIV_hash_bare": "tags: #private\n",                       # 回落解析器读成 '#private'
    "PRIV_nested": "tags: [private/journal]\n",
    "PRIV_flow_hash": "tags: [work, #private]\n",
    "PRIV_flow_hash_quoted": "tags: [\"#Private\", work]\n",
    "PRIV_upper": "tags: [PRIVATE]\n",
    "PRIV_many": "tags:\n" + _FILLER + "  - private\n",         # 第 40 个 tag：超出读端条数上限
    "PRIV_long_nested": "tags: [private/" + "x" * 200 + "]\n",  # 超出读端单个 tag 长度上限
    "PRIV_long_fm": "note: " + "x" * 2100 + "\ntags: [private]\n",  # frontmatter 超过旧的 2000 字符窗口（D-M1）
}
# 整篇原文：frontmatter 已开、写端读满上限（64 Ki 字符）仍没见到闭合行。cache 里没有它的 tags，读端按
# `fm_truncated` 判 tag 不全，精排两条外发通路都不送（D-M1）。文件名照样带 PRIV_，由 `_leaks` 一并检查。
RAW_NOTES = {
    "PRIV_unclosed": ("---\ntags: [private]\nsummary: 召回 SECRET-PRIV_unclosed\n"
                      + "".join(f"k{i}: {'v' * 60}\n" for i in range(1200))),
}
CONTROLS = {
    "PUBLIC_flow": "tags: [work]\n",
    "PUBLIC_block": "tags:\n  - work\n  - privatestuff\n",       # 前缀相同但不是 private 本身或其子 tag
}

_WRITER = """
import sys
sys.path.insert(0, sys.argv[1])
import rebuild_index as R
R._HAS_YAML = False
sys.argv = ["rebuild_index.py", "--vault", sys.argv[2], "--emit", "claude_md"]
try:
    R.main()
except SystemExit as exc:
    if exc.code not in (0, None):
        raise
"""

_READER = """
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
from scripts._config_loader import DEFAULT_CONFIG
from scripts._fine_config import get_value
from scripts._fine_input import build_fine_input, exclusion_set
from scripts._frontmatter_reader import load_cache_status
from scripts._topic import build_pin_prompt, build_supp_prompt

vault = pathlib.Path(sys.argv[2])
entries, status = load_cache_status(vault)
fet = get_value(DEFAULT_CONFIG, "fine_exclude_tags")
ent = list(DEFAULT_CONFIG["relevance"]["exclude_note_tags"])
cfg = {"catalog": True, "catalog_max_chars": 80000, "top_n": 30, "project_top_k": 0, "pool_max": 50,
       "fine_exclude_tags": fet, "exclude_note_tags": ent}

def run(injected):
    ex = exclusion_set(entries, exclude_note_tags=ent, fine_exclude_tags=fet, injected=injected, excluded=[])
    fin = build_fine_input(entries, words=["召回"], exclude=ex, project_rels=[], cfg=cfg, injected=injected,
                           work_titles=[], k_texts=[], degraded=[])
    return fin, [build_pin_prompt("召回 怎么调", fin, 1), build_supp_prompt("召回 怎么调", fin, 3)]

fin_c, cand_prompts = run([])
fin_i, ctx_prompts = run(sorted(entries))
print(json.dumps({"status": status.value, "n_entries": len(entries),
                  "cache_tags": {k: list(e.tags) for k, e in sorted(entries.items())},
                  "unknown": sorted(k for k, e in entries.items() if e.tags_unknown),
                  "candidates": [c.name for c in fin_c.candidates],
                  "in_context": list(fin_i.in_context),
                  "cand_prompts": cand_prompts, "ctx_prompts": ctx_prompts}))
"""


def _env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("GIT_") and k.upper() not in ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR")}
    env.update(HOME=str(home), USERPROFILE=str(home), CLAUDE_CONFIG_DIR=str(home / ".claude"),
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    return env


def _note(front: str, name: str) -> str:
    return f"---\n{front}summary: 召回 SECRET-{name}\n---\n# {name}\n正文\n"


@pytest.fixture(scope="module")
def outbound(tmp_path_factory):
    base = tmp_path_factory.mktemp("s_h1")
    home = base / "home"
    home.mkdir()
    vault = base / "vault"
    (vault / "笔记").mkdir(parents=True)
    (vault / ".meta").mkdir()
    for name, front in {**NOTES, **CONTROLS}.items():
        (vault / "笔记" / f"{name}.md").write_text(_note(front, name), encoding="utf-8")
    for name, text in RAW_NOTES.items():
        (vault / "笔记" / f"{name}.md").write_text(text, encoding="utf-8")
    env = _env(home)
    w = subprocess.run([sys.executable, "-B", "-c", _WRITER, str(SS), str(vault)], env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert w.returncode == 0, w.stderr
    assert (vault / ".meta" / "frontmatter-cache.json").is_file(), w.stderr
    r = subprocess.run([sys.executable, "-B", "-c", _READER, str(VL), str(vault)], env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    # 前置：cache 里每一篇都在（否则「没外发」可能只是「压根没读到」）
    assert data["status"] == "ok" and data["n_entries"] == len(NOTES) + len(CONTROLS) + len(RAW_NOTES), data
    return data


def _leaks(prompts) -> list:
    """prompt 里出现的带 private 的笔记名（PRIV_ 文件名或 SECRET- 摘要）。"""
    found = set()
    for text in prompts:
        for name in (*NOTES, *RAW_NOTES):
            if f"«{name}»" in text or f"SECRET-{name}»" in text:
                found.add(name)
    return sorted(found)


def test_candidate_path_sends_no_private_note(outbound):
    """候选通路（摘要层 + 目录层）：任何写法的 private 笔记，文件名与摘要都不进外发 prompt。"""
    leaks = _leaks(outbound["cand_prompts"])
    assert leaks == [], f"带 private 的笔记进了候选外发：{leaks}\ncache tags={outbound['cache_tags']}"
    assert not any(n.startswith("PRIV_") for n in outbound["candidates"])
    for text in outbound["cand_prompts"]:
        assert "PRIV_" not in text and "SECRET-PRIV" not in text
        for ctrl in CONTROLS:                               # 对照：不带 private 的笔记名与摘要仍外发
            assert f"«{ctrl}»：«召回 SECRET-{ctrl}»" in text, text


def test_frontmatter_window_shapes_take_their_branch(outbound):
    """D-M1 两种形态各走自己的分支：长 frontmatter 由写端读出 private；未闭合的没有 tag，由读端判 tag 不全。
    后者的对照是其余笔记——写端没读全的只有它，判不全的集合里不应多出别的长 frontmatter 笔记。"""
    tags = outbound["cache_tags"]
    assert tags["笔记/PRIV_long_fm.md"] == ["private"], tags
    assert tags["笔记/PRIV_unclosed.md"] == []
    assert "笔记/PRIV_unclosed.md" in outbound["unknown"]
    assert "笔记/PRIV_long_fm.md" not in outbound["unknown"]


def test_in_context_path_sends_no_private_note(outbound):
    """「已在上下文」通路：已注入的 private 笔记，文件名同样不外发。"""
    assert sorted(outbound["in_context"]) == sorted(CONTROLS), outbound["in_context"]
    leaks = _leaks(outbound["ctx_prompts"])
    assert leaks == [], f"带 private 的已注入笔记名进了外发：{leaks}"
    for text in outbound["ctx_prompts"]:
        assert "PRIV_" not in text
        for ctrl in CONTROLS:
            assert f"- «{ctrl}»" in text, text
