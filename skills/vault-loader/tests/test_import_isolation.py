# -*- coding: utf-8 -*-
"""UPS 的导入隔离（2.4.0 full-review A-H1）：单独兜底的模块加载失败，只丢精排或快照，粗排召回照常。

`prompt_submit_load` 的核心 try 块里任一模块（连同它们在模块级传递导入的模块）加载失败，整轮不注入
（`test_fail_open.py` 钉的那条契约）。`_metrics`、`_session_ops`、`_fine_flow`、`_fine_state`、`_fine_input`
在核心块之外各自兜底——前提是核心链不在模块级导入它们。2.4.0 曾经不满足：`_topic` 在模块级导入
`_fine_state` 与 `_fine_input`，后两者又各从 `_session_ops` 取一个符号，于是三处「单独兜底」的注释都不成立。

探针的做法：在**独立子进程**里装一个 `MetaPathFinder`，对被拦的模块名抛 `ImportError`，然后以
`__main__` 身份跑真实入口脚本（与宿主执行 hook 的方式一致），喂一份会命中笔记的 hook 输入。
- 隔离集逐个拦：必须仍有粗排注入；并核对拦截确实发生过（stderr 有探针标记），否则「没拦到」也会是绿的；
- 阳性对照：拦一个核心模块（`_scorer`），必须没有注入——证明探针能让核心链失败；
- 空白对照：拦一个不存在的模块名，必须有注入——证明探针本身不破坏入口。
另有一组只做导入、看 `sys.modules` 的用例：`_fine_config`、`_inject_format` 与整条核心链导入之后，
隔离集里的模块一个都不能出现。

子进程 PATH 指向空目录：精排默认开，不能让探针真拉起付费的 `claude`（`spawn_fine` 找不到它就记 no_claude）。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._neutral import NEUTRAL_CWD

VL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = VL_ROOT / "scripts" / "prompt_submit_load.py"
TIMEOUT = 60

# UPS 父进程里单独兜底的模块（隔离集）
ISOLATED = ("scripts._session_ops", "scripts._fine_state", "scripts._fine_input",
            "scripts._fine_flow", "scripts._metrics")

# 探针：拦截 argv[1] 指定的模块名，再以 __main__ 身份运行 argv[2] 指定的入口脚本
_RUNNER = "\n".join([
    "import importlib.abc, runpy, sys",
    "blocked, script = sys.argv[1], sys.argv[2]",
    "class _Block(importlib.abc.MetaPathFinder):",
    "    def find_spec(self, name, path=None, target=None):",
    "        if name == blocked:",
    "            sys.stderr.write('PROBE-BLOCKED ' + name + chr(10))",
    "            raise ImportError('probe-blocked ' + name)",
    "        return None",
    "sys.meta_path.insert(0, _Block())",
    "sys.argv = [script]",
    "runpy.run_path(script, run_name='__main__')",
])

# 只做导入：argv[1] 是 sys.path 根，argv[2] 是逗号分隔的隔离集，其后是要导入的模块
_IMPORT_ONLY = "\n".join([
    "import importlib, sys",
    "sys.path.insert(0, sys.argv[1])",
    "iso = set(sys.argv[2].split(','))",
    "for m in sys.argv[3:]:",
    "    importlib.import_module(m)",
    "print('LOADED=' + ','.join(sorted(m for m in sys.modules if m in iso)))",
])

NOTES = {
    "笔记/alpha.md": {"tags": ["hook"], "keywords": ["注入设计"],
                      "summary": "alpha 讲 hook 注入设计的取舍与实现细节，摘要足够长"},
    "笔记/红烧肉.md": {"tags": ["烹饪"], "keywords": [],
                      "summary": "红烧肉的做法与火候控制要点，摘要足够长足够长"},
}
PROMPT = "hook 注入设计 怎么做"
HIT = "「alpha」"


def _env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    empty = home / "emptybin"
    empty.mkdir(exist_ok=True)
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PYTHONUTF8": "1",
                "CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": str(empty)})
    env.pop("VAULT_LOADER_DISABLE", None)
    return env


@pytest.fixture
def ups_home(tmp_home: Path, tmp_vault: Path, write_frontmatter_cache) -> Path:
    entries = {}
    for rel, meta in NOTES.items():
        p = tmp_vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {Path(rel).stem}\n\n正文\n", encoding="utf-8")
        entries[rel] = {"category": "笔记", "mtime": 1900000000, **meta}
    write_frontmatter_cache(entries)
    d = tmp_home / ".context-vault"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({"vault_path": str(tmp_vault)}, ensure_ascii=False),
                                   encoding="utf-8")
    return tmp_home


def _run_blocked(home: Path, blocked: str) -> subprocess.CompletedProcess:
    payload = {"cwd": str(NEUTRAL_CWD), "prompt": PROMPT, "session_id": "sess-import-iso",
               "prompt_id": "pid-import-iso", "hook_event_name": "UserPromptSubmit",
               "transcript_path": ""}
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, blocked, str(SCRIPT)],
        input=json.dumps(payload, ensure_ascii=False), capture_output=True, text=True,
        encoding="utf-8", env=_env(home), cwd=str(home), timeout=TIMEOUT)


def _context(r: subprocess.CompletedProcess) -> str:
    out = r.stdout.strip()
    if not out:
        return ""
    return (json.loads(out).get("hookSpecificOutput") or {}).get("additionalContext") or ""


@pytest.mark.parametrize("blocked", ISOLATED)
def test_isolated_module_import_failure_keeps_coarse_recall(ups_home: Path, blocked: str) -> None:
    r = _run_blocked(ups_home, blocked)
    assert r.returncode == 0, r.stderr
    assert f"PROBE-BLOCKED {blocked}" in r.stderr, f"拦截没有发生，用例测不到隔离：{r.stderr}"
    assert "模块加载失败" not in r.stderr, f"{blocked} 加载失败拖垮了核心导入链：{r.stderr}"
    assert HIT in _context(r), f"拦截 {blocked} 后粗排没有注入。stderr：{r.stderr}"


def test_positive_control_core_module_failure_drops_recall(ups_home: Path) -> None:
    """阳性对照：拦一个核心模块，必须整轮不注入。证明上面那组用例的「仍有注入」不是探针失效造成的。"""
    r = _run_blocked(ups_home, "scripts._scorer")
    assert r.returncode == 0, r.stderr
    assert "PROBE-BLOCKED scripts._scorer" in r.stderr
    assert "模块加载失败" in r.stderr, r.stderr
    assert r.stdout.strip() == "" and _context(r) == ""


def test_blank_control_nonexistent_module_keeps_recall(ups_home: Path) -> None:
    """空白对照：拦一个谁都不导入的名字，入口照常注入。证明探针本身不破坏入口。"""
    r = _run_blocked(ups_home, "scripts._import_iso_probe_nonexistent")
    assert r.returncode == 0, r.stderr
    assert "PROBE-BLOCKED" not in r.stderr
    assert HIT in _context(r), r.stderr


def _core_chain() -> list:
    """`prompt_submit_load` 核心 try 块里导入的模块（按源码取，新加的导入自动纳入）。"""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Try):
            continue
        mods = [n.module for n in node.body if isinstance(n, ast.ImportFrom) and n.module]
        if "scripts._config_loader" in mods:
            return mods
    raise AssertionError("找不到 prompt_submit_load 的核心导入块")


def _loaded_after_import(modules) -> str:
    r = subprocess.run(
        [sys.executable, "-c", _IMPORT_ONLY, str(VL_ROOT), ",".join(ISOLATED), *modules],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"}, cwd=str(VL_ROOT), timeout=TIMEOUT)
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("LOADED=")]
    assert len(lines) == 1, r.stdout
    return lines[0][len("LOADED="):]


@pytest.mark.parametrize("module", ["scripts._fine_config", "scripts._inject_format"])
def test_coarse_prerequisites_do_not_pull_isolated_modules(module: str) -> None:
    """`_fine_config`、`_inject_format` 留在核心链上（粗排的开关与渲染要用），所以它们不得在模块级导入隔离集。"""
    assert _loaded_after_import([module]) == ""


def test_core_chain_does_not_pull_isolated_modules() -> None:
    mods = _core_chain()
    assert len(mods) >= 10 and "scripts._topic" in mods, mods        # 取到的确是核心块
    assert not set(mods) & set(ISOLATED), f"隔离集里的模块出现在核心导入块：{set(mods) & set(ISOLATED)}"
    assert _loaded_after_import(mods) == ""


# 隔离集内部允许的模块级依赖（2.4.0 full-review 修复批次 2 fix round 1，按当前代码逐个单独导入实测）。
# 上面的探针只断言粗排：隔离模块之间多出一条依赖时，被依赖的那个加载失败会连带依赖它的那个一起丢，
# 粗排照样注入，探针看不出来（controller 对照变异 C1：`_fine_state` 改回从 `_session_ops` 取链接判据，
# 整个文件照样全绿）。这里把「谁可以带走谁」写死：表外多出一条即红，新增依赖必须先在这里登记理由。
ISOLATED_DEPS = {
    # 快照与 mod 请求；链接判据取 `_state.is_link_like`，不依赖任何精排模块
    "scripts._session_ops": frozenset(),
    # fine 状态读写；链接判据取 `_state.is_link_like`（不从 `_session_ops` 取，快照模块失败不得带走精排状态）
    "scripts._fine_state": frozenset(),
    # 精排输入与排除集；不可见字符由 `_output._CF_RE` 剥（不借 `_session_ops._INVISIBLE_RE`），不依赖任何隔离模块
    "scripts._fine_input": frozenset(),
    # 每轮的精排编排：`FineState` 与 fine 状态的读写都在 `_fine_state`。二者在 `prompt_submit_load` 的同一个
    # 兜底块里导入，任一失败两者本来就一起为 None，这条依赖不扩大影响面
    "scripts._fine_flow": frozenset({"scripts._fine_state"}),
    # 决策面指标；不依赖任何隔离模块（UPS 另有 `_MetricsStub` 替身）
    "scripts._metrics": frozenset(),
}


def test_isolated_deps_table_covers_isolated_set() -> None:
    assert set(ISOLATED_DEPS) == set(ISOLATED)


@pytest.mark.parametrize("module", ISOLATED)
def test_isolated_modules_only_pull_declared_isolated_deps(module: str) -> None:
    """隔离集的模块之间只允许 `ISOLATED_DEPS` 登记过的模块级依赖：单独导入一个，`sys.modules` 里出现的其他
    隔离模块必须在它的允许表里。"""
    loaded = {m for m in _loaded_after_import([module]).split(",") if m} - {module}
    extra = loaded - ISOLATED_DEPS[module]
    assert not extra, (f"{module} 在模块级带进了未登记的隔离模块 {sorted(extra)}："
                       f"它们加载失败会连带 {module} 一起丢。确需依赖就在 ISOLATED_DEPS 登记并写明理由")


def test_isolated_deps_probe_sees_a_real_dependency() -> None:
    """阳性对照：`_fine_flow` → `_fine_state` 这条已登记的依赖必须被看见，证明上面那组用例的「没有多余依赖」
    不是因为 `_loaded_after_import` 看不见隔离模块之间的导入。"""
    assert "scripts._fine_state" in _loaded_after_import(["scripts._fine_flow"]).split(",")
