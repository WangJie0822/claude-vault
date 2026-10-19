"""metrics 记录的**落点**守卫：两个 hook 入口必须写进同一个命名空间。

为什么必须端到端（真起子进程）跑入口，而不能调纯函数：
`_metrics._RUNTIME` 是**进程级全局**，默认值写死 `"legacy"`
（`_metrics.py` 模块顶部），只有入口显式调 `_metrics.configure_context()`
才会切到 canonical。落点因此只在「入口 → configure → metrics_dir()」这条
完整链路里暴露。

`tests/test_session_start_metrics.py` 的 7 条用例全部直接调
`build_session_start_record` / `summarize` / `render_report` 这类纯函数，
对「`metrics_dir()` 用了哪个根」**结构性不可见** —— 于是
`session_start_load.py` 自引入 metrics 起就漏调 `_metrics.configure_context`，
135 条 session_start 记录全部写进 0.9.x 的 `~/.claude/vault-loader-metrics/`，
而 `analyze_metrics` 在 canonical 下读 `~/.context-vault/metrics/<runtime>/`
（该 channel 计数恒为 0），全程零报错。2026-09-16 实证并修复。

⚠️ 2.0.0 起宿主恒为 Claude，payload 不再影响命名空间。payload 仍带 `hook_event_name`
是为了与真实入参同形——缺它时入口不做事件去重，走的是另一条分支。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SS_SCRIPT = SCRIPTS / "session_start_load.py"
UPS_SCRIPT = SCRIPTS / "prompt_submit_load.py"

LEGACY_REL = (".claude", "vault-loader-metrics")
CANONICAL_REL = (".context-vault", "metrics", "claude")


def _run(script: Path, payload: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    # 显式置为交互式：_entrypoint.is_supported_session() 只放行 "cli"。
    # 不设则继承跑测试的那个会话 —— 在 `claude -p` 下跑测试时入口会静默早退，
    # 用例变成「什么都没发生也绿」。
    env["CLAUDE_CODE_ENTRYPOINT"] = "cli"
    # 剥掉两个 root 变量：2.0.0 起它们不再影响命名空间，剥离只是为了让用例不依赖
    # 跑测试那台机器的环境。
    env.pop("PLUGIN_ROOT", None)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", env=env, timeout=30,
    )


def _records(root: Path) -> list[dict]:
    """读出某个 metrics 根下的全部记录（不含顶层辅助文件）。"""
    out: list[dict] = []
    if not root.is_dir():
        return out
    for month in root.iterdir():
        if not month.is_dir():
            continue
        for f in month.glob("*.jsonl"):
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def _write_config(home: Path, vault: Path) -> None:
    cfg_dir = home / ".context-vault"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # 关精排：本文件只验 metrics 落点。精排默认开，而 `_run` 复制的是全量环境（真实 PATH，可能还有注入的
    # provider 令牌），UPS 用例会真起一条 detached 的付费 `claude -p`（2.4.0 起无命中的首轮也发起点名）。
    (cfg_dir / "config.json").write_text(
        json.dumps({"vault_path": str(vault), "metrics": {"enabled": True},
                    "relevance": {"fine_rank": False}}),
        encoding="utf-8")


def _seed_vault(vault: Path, write_frontmatter_cache, project: str) -> None:
    """给 SessionStart 造出可注入内容，使其走到 metrics 落盘那一步。

    SessionStart 在无内容时会提前 `_finish` 返回、根本不写 metrics，
    那样用例对落点零判别力。
    """
    proj_dir = vault / "项目笔记" / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "design.md").write_text("# design", encoding="utf-8")
    write_frontmatter_cache({
        f"项目笔记/{project}/design.md": {
            "tags": ["设计"], "category": "项目笔记",
            "summary": "项目设计", "mtime": 1900000000,
        }
    })


def test_session_start_metrics_land_in_canonical_namespace(
        tmp_home: Path, tmp_vault: Path, tmp_git_repo: Path,
        write_frontmatter_cache) -> None:
    """SessionStart 的记录必须落 canonical，不得落 0.9.x 的 legacy 根。

    这是原缺陷的直接守卫：把 `session_start_load.py` 里那行
    `_m.configure_context(...)` 删掉，本用例转红。
    """
    _write_config(tmp_home, tmp_vault)
    _seed_vault(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)

    r = _run(SS_SCRIPT, {
        "cwd": str(tmp_git_repo),
        "hook_event_name": "SessionStart",
        "session_id": "sess-ns-1",
        "source": "startup",
    })
    assert r.returncode == 0, f"hook 非 0 退出：{r.stderr}"

    canonical = _records(tmp_home.joinpath(*CANONICAL_REL))
    legacy = _records(tmp_home.joinpath(*LEGACY_REL))

    ss_canon = [x for x in canonical if x.get("channel") == "session_start"]
    ss_legacy = [x for x in legacy if x.get("channel") == "session_start"]

    assert ss_canon, (
        "canonical 下没有 session_start 记录；"
        f"legacy 下有 {len(ss_legacy)} 条 —— 入口漏调 _metrics.configure_context"
    )
    assert not ss_legacy, f"session_start 记录落进了 legacy 根：{ss_legacy}"
    # runtime 字段只在 _RUNTIME != "legacy" 时写入，是落点的第二个独立判据：
    # 光看目录可能被「两边都写」之类的实现蒙混过去。
    assert ss_canon[0].get("runtime") == "claude", ss_canon[0]


def test_both_hook_entries_share_one_namespace(
        tmp_home: Path, tmp_vault: Path, tmp_git_repo: Path,
        write_frontmatter_cache) -> None:
    """两个入口的记录必须落在**同一个**根下。

    本用例的独立价值：只破坏 UPS 一侧时，上一条用例通过而本条失败 —— 它钉的是
    「一个入口改了、另一个忘了」这种分叉，正是原缺陷的形态（UPS 侧一直是对的，
    SessionStart 侧漏了）。

    ⚠️ 两条断言的稳定性不同，别把本用例整体当成「与命名空间无关」：
      * `assert not legacy` 确实与具体命名空间无关 —— 无论默认根将来迁到哪里，
        都不该有记录掉进 0.9.x 的 legacy 根；
      * `assert canon_ss and canon_ups` **锚定当前的 canonical 根**
        （`CANONICAL_REL`）。默认命名空间若整体迁移到第三个根，这一条要跟着改。
    此处原写作「判据刻意不写死 canonical，将来整体迁移仍成立」，与实现不符
    （`CANONICAL_REL` 就硬编码在正向断言里），已按实际行为订正。
    """
    _write_config(tmp_home, tmp_vault)
    _seed_vault(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)

    ss = _run(SS_SCRIPT, {
        "cwd": str(tmp_git_repo),
        "hook_event_name": "SessionStart",
        "session_id": "sess-ns-2",
        "source": "startup",
    })
    assert ss.returncode == 0, ss.stderr
    ups = _run(UPS_SCRIPT, {
        "cwd": str(tmp_git_repo),
        "hook_event_name": "UserPromptSubmit",
        "session_id": "sess-ns-2",
        "prompt_id": "p-1",
        "prompt": "项目设计 相关的历史决策",
    })
    assert ups.returncode == 0, ups.stderr

    canonical = _records(tmp_home.joinpath(*CANONICAL_REL))
    legacy = _records(tmp_home.joinpath(*LEGACY_REL))

    def _split(recs: list[dict]) -> tuple[int, int]:
        ss_n = sum(1 for x in recs if x.get("channel") == "session_start")
        return ss_n, len(recs) - ss_n

    canon_ss, canon_ups = _split(canonical)
    leg_ss, leg_ups = _split(legacy)

    assert canon_ss and canon_ups, (
        "两个入口没有都落到 canonical："
        f"canonical(ss={canon_ss}, ups={canon_ups}) legacy(ss={leg_ss}, ups={leg_ups})"
    )
    assert not legacy, (
        "有记录落进了 legacy 根，两个入口的命名空间不一致："
        f"legacy(ss={leg_ss}, ups={leg_ups})"
    )


def _plugin_copy_with_broken_metrics(tmp_path: Path) -> Path:
    """复制整个插件根，只把 `_metrics.py` 换成导入期必炸的版本。

    必须复制**整个插件根**而不只是 `scripts/`：SessionStart 要靠 `context_vault`
    构造 HookContext，拿不到它 `hook_context` 就是 None、`_state` 那条配置分支
    根本不执行 —— 用例会因为「压根没走到被测路径」而恒绿，失去判别力。
    """
    dst = tmp_path / "plugin"
    ignore = shutil.ignore_patterns("__pycache__", ".git", "tests", "docs")
    shutil.copytree(PLUGIN_ROOT / "skills" / "vault-loader",
                    dst / "skills" / "vault-loader", ignore=ignore)
    shutil.copytree(PLUGIN_ROOT / "context_vault", dst / "context_vault",
                    ignore=ignore)
    (dst / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    shutil.copy(PLUGIN_ROOT / ".claude-plugin" / "plugin.json",
                dst / ".claude-plugin" / "plugin.json")
    (dst / "skills" / "vault-loader" / "scripts" / "_metrics.py").write_text(
        "raise ImportError('simulated metrics import failure for test guard')\n",
        encoding="utf-8")
    return dst / "skills" / "vault-loader" / "scripts" / "session_start_load.py"


def test_broken_metrics_import_does_not_drag_state_into_legacy(
        tmp_path: Path, tmp_home: Path, tmp_vault: Path, tmp_git_repo: Path,
        write_frontmatter_cache) -> None:
    """`_metrics` 导入失败不得连累 `_state` 的命名空间配置。

    回归钉子。第一版修复把 `from scripts import _metrics as _m` 放进了与
    `_state.configure_context()` **同一个** try 块、且在它**之前**：`_metrics`
    一旦导入失败，`_state` 的配置被一起跳过，注入去重 state 从 canonical 掉进
    `~/.claude/projects/<hash>/vault-loader-state.json`。实测确认后改为把
    `_m.configure_context` 下沉进 metrics 自己的分支。

    UPS 入口能把两者并列，是因为它有模块级 import + `_MetricsStub` 兜底；本入口
    是惰性 import，没有那层替身，所以位置不能照搬。

    变异：把 `_m.configure_context(...)` 连同 import 挪回上面那个 try 块，本用例转红。
    """
    _write_config(tmp_home, tmp_vault)          # metrics 开启，否则根本不 import
    _seed_vault(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    script = _plugin_copy_with_broken_metrics(tmp_path)

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env["CLAUDE_CODE_ENTRYPOINT"] = "cli"
    r = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps({
            "cwd": str(tmp_git_repo),
            "hook_event_name": "SessionStart",
            "session_id": "sess-broken-metrics",
            "source": "startup",
        }),
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)

    assert r.returncode == 0, f"fail-open 被破坏：rc={r.returncode} stderr={r.stderr!r}"

    canonical_state = list((tmp_home / ".context-vault" / "state").rglob("*.json")) \
        if (tmp_home / ".context-vault" / "state").is_dir() else []
    legacy_state = list((tmp_home / ".claude" / "projects").rglob(
        "vault-loader-state.json")) if (tmp_home / ".claude" / "projects").is_dir() else []

    assert canonical_state, (
        "metrics 导入失败把 state 一起带走了：canonical 下没有 state 文件，"
        f"legacy 下有 {len(legacy_state)} 个。stderr={r.stderr!r}")
    assert not legacy_state, (
        f"state 落进了 legacy 布局：{[str(p) for p in legacy_state]}")
