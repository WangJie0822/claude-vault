"""rebuild_index：summary_src 与摘要缺口统计（spec §3.1、§5）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
GOOD = "这是一篇正常笔记的摘要说明了解决的问题与最终的结论"


def _w(v: Path, rel: str, text: str):
    p = v / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _build(tmp_path) -> Path:
    v = tmp_path / "V"
    (v / ".meta").mkdir(parents=True)
    _w(v, "技术笔记/ok.md", f'---\ncategory: 技术笔记\nsummary: "{GOOD}"\n---\n正文\n')
    _w(v, "技术笔记/h1.md", "---\ncategory: 技术笔记\n---\n# 一个足够长的标题会被兜底当成摘要来显示\n")
    _w(v, "技术笔记/short.md", '---\ncategory: 技术笔记\nsummary: "太短"\n---\n')
    _w(v, "技术笔记/fname.md", '---\ncategory: 技术笔记\nsummary: "fname"\n---\n')
    _w(v, "技术笔记/long.md", '---\ncategory: 技术笔记\nsummary: "' + "长" * 241 + '"\n---\n')
    _w(v, "工作日志/2026年/10月/2026-10-13.md", '---\ncategory: 工作日志\nsummary: "2026-10-13 工作记录"\n---\n')
    _w(v, "工作日志/misfiled.md", '---\ncategory: 项目笔记\nsummary: "x"\n---\n')   # 路径命中工作日志
    _w(v, "技术笔记/nofm.md", "# 没有 frontmatter 的笔记\n")
    _w(v, "Claude Code/specs/s.md", '---\ncategory: Claude Code\ntags: [spec, archived]\nsummary: "短"\n---\n')
    _w(v, "技术笔记/arch.md", '---\ncategory: 技术笔记\nstatus: archived\nsummary: "短"\n---\n')
    return v


def _rebuild(v: Path) -> dict:
    r = subprocess.run([sys.executable, str(SCRIPTS / "rebuild_index.py"), "--vault", str(v)],
                       capture_output=True, text=True, encoding="utf-8",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))   # rebuild_index 输出跟随控制台编码
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_cache_records_summary_src(tmp_path):
    v = _build(tmp_path)
    _rebuild(v)
    e = json.loads((v / ".meta" / "frontmatter-cache.json").read_text(encoding="utf-8"))["entries"]
    assert e["技术笔记/ok.md"]["summary_src"] == "fm"
    assert e["技术笔记/h1.md"]["summary_src"] == "h1"
    assert e["技术笔记/h1.md"]["summary"].startswith("一个足够长的标题")


def test_health_check_summary_counts(tmp_path):
    v = _build(tmp_path)
    hc = _rebuild(v)["health_check"]
    # 缺口：h1(missing) + short + fname(filename) = 3；归集文档、status archived、工作日志、无 fm 都不算
    assert hc["summary_gap"] == 3
    assert sorted(hc["summary_gap_samples"]) == ["技术笔记/fname.md", "技术笔记/h1.md", "技术笔记/short.md"]
    assert hc["summary_optimizable"] == 1
    assert hc["summary_no_frontmatter"] == 1
    assert hc["summary_coverage"] == (5 - 3) * 100 // 5


def test_keywords_exemption_uses_path_based_worklog(tmp_path):
    v = _build(tmp_path)
    _rebuild(v)
    from rebuild_index import _health_check
    entries = json.loads((v / ".meta" / "frontmatter-cache.json").read_text(encoding="utf-8"))["entries"]
    issues = _health_check(entries, v, indexes_written=[])
    assert "工作日志/misfiled.md" not in issues["keywords_applicable"]
    assert "工作日志/misfiled.md" not in issues["summary_applicable"]
