"""tests for scripts/keywords_gap.py —— /summarize-session 流程内的 keywords 缺口补全。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from keywords_gap import main, resolve_in_vault, set_keywords  # noqa: E402


def _note(path: Path, *, category: str, keywords=None, frontmatter=True, nl="\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not frontmatter:
        path.write_text(f"# 无 frontmatter{nl}{nl}正文", encoding="utf-8", newline="")
        return path
    lines = ["---", f"category: {category}", "tags: [x]", 'summary: "s"']
    if keywords:
        lines.append("keywords: [" + ", ".join(keywords) + "]")
    lines += ["---", "# 标题", "", "正文内容"]
    path.write_text(nl.join(lines) + nl, encoding="utf-8", newline="")
    return path


def _build_vault(base: Path) -> Path:
    """混合 vault：工作日志(豁免) / 有 kw / 缺 kw / 无 frontmatter 四类都有。

    四类齐备是刻意的——只有缺 kw 的话，分子分母口径错了也看不出来。
    """
    v = base / "V"
    v.mkdir()
    for i in range(4):
        _note(v / "工作日志" / "2026年" / "08月" / f"2026-08-0{i + 1}.md",
              category="工作日志")
    for i in range(3):
        _note(v / "技术笔记" / f"has{i}.md", category="技术笔记",
              keywords=["召回打分", "recall"])
    for i in range(5):
        _note(v / "项目笔记" / "demo" / "specs" / f"lack{i}.md", category="项目笔记")
    _note(v / "技术笔记" / "nofm.md", category="", frontmatter=False)
    return v


def _run(script: str, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True, text=True, encoding="utf-8")


def _rebuild(vault: Path) -> dict:
    r = _run("rebuild_index.py", "--vault", str(vault))
    return json.loads(r.stdout.strip().splitlines()[-1])


def _list_gap(vault: Path, *extra) -> dict:
    r = _run("keywords_gap.py", "--vault", str(vault), "--list", *extra)
    return json.loads(r.stdout.strip().splitlines()[-1])


# ========== 口径一致性（防漂移，本文件最要紧的一条） ==========

def test_list_total_matches_rebuild_index_coverage_numerator(tmp_path):
    """--list 的 total 必须逐一等于 rebuild_index 报告的 keywords_missing。

    两者若各算各的，就会出现「报告说缺 5 篇、补全只处理 3 篇」这种没人能发现的
    偏差：覆盖率永远回不到 100%，而补全每次都说自己做完了。本用例是这条契约的
    唯一守卫——keywords_gap.find_missing 复用 _health_check 正是为了它。
    """
    vault = _build_vault(tmp_path)
    report = _rebuild(vault)
    gap = _list_gap(vault)

    assert report["health_check"]["keywords_missing"] == 5
    assert gap["status"] == "ok"
    assert gap["total"] == report["health_check"]["keywords_missing"], (
        "两处口径必须一致；不一致说明 find_missing 没走 _health_check")
    assert len(gap["missing"]) == 5
    # 工作日志豁免、有 kw 的不进、无 frontmatter 的不进（后者另计 no_frontmatter）
    assert all("项目笔记" in p for p in gap["missing"])


def test_list_errors_when_cache_missing_not_silently_empty(tmp_path):
    """cache 缺失 → 报错退出，不得静默返回空清单。

    空清单与「真的没有缺口」在输出上无法区分，是最典型的假通过：
    流程会当作补全完成继续走下去。
    """
    vault = _build_vault(tmp_path)   # 刻意不跑 rebuild_index，无 cache
    gap = _list_gap(vault)
    assert gap["status"] == "error"
    assert "rebuild_index" in gap["reason"]


def test_limit_truncates_list_but_total_stays_full(tmp_path):
    """--limit 只截断返回条数，total 仍是全量——否则流程会误判缺口已清完。"""
    vault = _build_vault(tmp_path)
    _rebuild(vault)
    gap = _list_gap(vault, "--limit", "2")
    assert len(gap["missing"]) == 2
    assert gap["total"] == 5


# ========== --set 写入 ==========

def test_set_writes_keywords_and_rebuild_sees_it(tmp_path):
    """写入后，rebuild_index 的缺口计数应当真的减少 —— 端到端而非只看返回值。"""
    vault = _build_vault(tmp_path)
    _rebuild(vault)
    target = "项目笔记/demo/specs/lack0.md"
    r = _run("keywords_gap.py", "--vault", str(vault),
             "--set", target, "--keywords", "召回打分,tag-IDF,recall scoring")
    out = json.loads(r.stdout.strip())
    assert out["status"] == "ok"
    assert out["keywords"] == ["召回打分", "tag-IDF", "recall scoring"]

    text = (vault / target).read_text(encoding="utf-8")
    assert "keywords: [召回打分, tag-IDF, recall scoring]" in text
    # 端到端：缺口真的少了一篇
    assert _rebuild(vault)["health_check"]["keywords_missing"] == 4


def test_set_syncs_cache_entry_immediately(tmp_path):
    """写入后 cache 必须**立刻**带上新 keywords，不能指望下一次 rebuild 去读。

    rebuild_index 的增量判据是秒级 `int(st_mtime)`（rebuild_index.py:163），而
    「补全 → rebuild」在流程里通常发生在同一秒内 —— 它会判定文件未变、跳过重读。
    cache 不同步的后果不是慢一拍，而是**补了等于白补**：vault-loader 读的是
    cache，召回永远拿不到 keywords，下次会话的报告还会把它再列一遍。
    """
    vault = _build_vault(tmp_path)
    _rebuild(vault)
    cache = vault / ".meta" / "frontmatter-cache.json"
    rel = "项目笔记/demo/specs/lack3.md"
    before = json.loads(cache.read_text(encoding="utf-8"))["entries"][rel]
    assert not before.get("keywords"), "前置条件：该条目本应没有 keywords"

    res = set_keywords(vault, rel, ["召回打分", "recall"])
    assert res["status"] == "ok"
    assert res["cache_synced"] is True

    after = json.loads(cache.read_text(encoding="utf-8"))["entries"][rel]
    assert after["keywords"] == ["召回打分", "recall"], (
        "cache 未同步：同秒内的 rebuild 会跳过重读，这篇的召回不会生效")


def test_set_rejects_path_outside_vault(tmp_path):
    """`../` 穿越必须被挡——target 来自清单，但清单可能被人工编辑过。"""
    vault = _build_vault(tmp_path)
    outsider = tmp_path / "outside.md"
    _note(outsider, category="技术笔记")
    before = outsider.read_text(encoding="utf-8")

    res = set_keywords(vault, "../outside.md", ["召回打分", "recall"])
    assert res["status"] == "error"
    assert "outside vault" in res["reason"]
    assert outsider.read_text(encoding="utf-8") == before, "越界目标不得被改写"


def test_set_sanitizes_illegal_keywords(tmp_path):
    """非法词必须在写盘前被剔除，否则 YAML 元字符会就地破坏 frontmatter。"""
    vault = _build_vault(tmp_path)
    target = "项目笔记/demo/specs/lack1.md"
    res = set_keywords(vault, target, ["正常词", "a: b", "x", "has\nnewline"])
    assert res["status"] == "ok"
    assert res["keywords"] == ["正常词"]
    text = (vault / target).read_text(encoding="utf-8")
    assert "keywords: [正常词]" in text
    assert "a: b" not in text


def test_set_all_illegal_is_skipped_not_written(tmp_path):
    """全部非法 → skipped，且文件一个字节都不能动。"""
    vault = _build_vault(tmp_path)
    target = vault / "项目笔记/demo/specs/lack2.md"
    before = target.read_text(encoding="utf-8")
    res = set_keywords(vault, "项目笔记/demo/specs/lack2.md", ["x", "y", ""])
    assert res["status"] == "skipped"
    assert target.read_text(encoding="utf-8") == before


def test_set_without_frontmatter_is_skipped(tmp_path):
    """无 frontmatter 的文件不得被强行加上——那会改变它的性质。"""
    vault = _build_vault(tmp_path)
    res = set_keywords(vault, "技术笔记/nofm.md", ["召回打分", "recall"])
    assert res["status"] == "skipped"
    assert res["reason"] == "no frontmatter"


def test_set_preserves_crlf_line_endings(tmp_path):
    """CRLF 笔记写回后仍是 CRLF——归一会让整个文件在 git diff 里全行变更。"""
    vault = _build_vault(tmp_path)
    target = vault / "项目笔记/demo/crlf.md"
    _note(target, category="项目笔记", nl="\r\n")
    raw_before = target.read_bytes()
    assert raw_before.count(b"\r\n") > 0

    res = set_keywords(vault, "项目笔记/demo/crlf.md", ["召回打分", "recall"])
    assert res["status"] == "ok"
    raw = target.read_bytes()
    assert "keywords: [召回打分, recall]".encode("utf-8") in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), "不得混入裸 LF"


def test_set_replaces_existing_keywords_not_duplicate(tmp_path):
    """已有 keywords 的笔记被 --set 时替换而非追加第二行。"""
    vault = _build_vault(tmp_path)
    target = "技术笔记/has0.md"
    res = set_keywords(vault, target, ["新词一", "新词二"])
    assert res["status"] == "ok"
    text = (vault / target).read_text(encoding="utf-8")
    assert text.count("keywords:") == 1
    assert "新词一" in text and "召回打分" not in text


def test_resolve_in_vault_rejects_directory(tmp_path):
    """目录不是可写目标。"""
    vault = _build_vault(tmp_path)
    assert resolve_in_vault(vault, "技术笔记") is None


def test_set_requires_keywords_arg(tmp_path):
    """--set 缺 --keywords 直接报错，不静默成功。"""
    vault = _build_vault(tmp_path)
    rc = main(["--vault", str(vault), "--set", "a.md"])
    assert rc == 1


def test_list_and_set_are_mutually_exclusive(tmp_path):
    """--list 与 --set 互斥：同时给会被 argparse 拒掉，不会先列后写。"""
    vault = _build_vault(tmp_path)
    with pytest.raises(SystemExit):
        main(["--vault", str(vault), "--list", "--set", "a.md"])
