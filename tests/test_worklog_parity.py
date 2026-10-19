"""工作日志判据两份实现的对拍（spec 2.4.0 W1）。

写端 `skills/summarize-session/scripts/_summary.is_worklog(rel, entry_dict)` 判 cache 原始 dict；
读端 `skills/vault-loader/scripts/_candidate_pool.is_worklog(path, Entry)` 判 `load_cache_status` 产出的
Entry。两个 skill 不互相导入，且仓库根也有一个 `scripts` 包（与 vault-loader 的顶层包名相撞），所以两侧
各起一个子进程、在各自的导入环境里跑同一批样本。读端走「写 cache 文件 → load_cache_status → is_worklog」
完整链路：category 列表形态的归一发生在读 cache 那一步，只测函数本体会漏掉它。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SS = ROOT / "skills" / "summarize-session" / "scripts"
VL = ROOT / "skills" / "vault-loader"

# (cache 键, cache 原始条目)；键互不相同（读端要写进同一份 cache）
CASES = [
    ["工作日志/2026年/10月/2026-10-14.md", {}],
    ["项目笔记/a.md", {"category": "工作日志"}],
    ["项目笔记/b.md", {"category": ["技术笔记", "工作日志"]}],
    ["项目笔记/c.md", {"category": ["技术笔记"]}],
    ["项目笔记/工作日志.md", {"category": "技术笔记"}],
    ["工作日志.md", {}],
    ["工作日志x/d.md", {}],
    ["项目笔记/e.md", {"category": ""}],
    ["项目笔记/f.md", {"category": 123}],
    ["项目笔记/g.md", {"category": ["工作日志 "]}],
    ["项目笔记/h.md", {"category": [1, "工作日志"]}],
    ["技术笔记/工作日志/i.md", {}],
]
EXPECTED = [True, True, True, False, False, False, False, False, False, False, True, False]

_WRITER = """
import json, sys
sys.path.insert(0, sys.argv[1])
from _summary import is_worklog
cases = json.loads(sys.stdin.read())
print(json.dumps([bool(is_worklog(rel, meta)) for rel, meta in cases]))
"""

_READER = """
import json, sys, tempfile, pathlib
sys.path.insert(0, sys.argv[1])
from scripts._frontmatter_reader import load_cache_status
from scripts._candidate_pool import is_worklog
cases = json.loads(sys.stdin.read())
d = pathlib.Path(tempfile.mkdtemp())
(d / ".meta").mkdir()
entries = {rel: dict(meta, mtime=1, summary="s") for rel, meta in cases}
(d / ".meta" / "frontmatter-cache.json").write_text(
    json.dumps({"_version": 1, "entries": entries}, ensure_ascii=False), encoding="utf-8")
got, _status = load_cache_status(d)
print(json.dumps([bool(rel in got and is_worklog(rel, got[rel])) for rel, _m in cases]))
"""


def _run(code: str, path: Path) -> list:
    # -I 下 PYTHONIOENCODING 不生效：入参用 ensure_ascii 的 JSON（纯 ASCII），输出是布尔列表（纯 ASCII），
    # 与控制台编码无关。
    r = subprocess.run([sys.executable, "-I", "-c", code, str(path)],
                       input=json.dumps(CASES), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_worklog_judgement_parity():
    writer = _run(_WRITER, SS)
    reader = _run(_READER, VL)
    assert writer == EXPECTED, f"写端判据与预期不符：{writer}"
    assert reader == writer, f"读写两侧分叉：writer={writer} reader={reader}"
    assert True in reader and False in reader    # 阳性对照：样本两类都有
