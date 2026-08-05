#!/usr/bin/env python3
"""SessionStart hook 入口（方案 B''）。

读 stdin JSON（含 cwd 字段），输出「项目固定上下文」分组清单到 stdout：
项目相关笔记（项目目录 ∪ 标签匹配，按 mtime 倒序）+ 近期工作日志 + 近期 git 提交。
不做跨 vault 打分排序；关键词相关笔记由 UserPromptSubmit(J) 按需加载。
失败默认静默退出（exit 0、stdout 空）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# 确保能 import 同级模块
sys.path.insert(0, str(Path(__file__).parent.parent))

# fail-open 硬约束的一个既有缺口（F6）：顶层 import 在下方 `if __name__ == "__main__"`
# 的 try/except 之外执行，任何导入期异常都会直接 exit 1 + traceback，兜底完全覆盖不到
# （实测 EXIT=1）。以脚本身份运行时降级为静默 exit 0；被测试 import 时仍原样抛出。
try:
    from scripts._config_loader import load_config_ex, compare_vault_paths
    from scripts._frontmatter_reader import load_cache_status
    from scripts._output import emit, approx_size_str, sanitize_injected_text, INJECTION_NOTICE
    from scripts._scorer import is_archived
    from scripts._vault_init import ensure_vault_if_default
    from scripts._signal_collect import (
        collect_recent_commits,
        collect_signal_a_project_dir,
        collect_signal_b_keyword_map,
        collect_signal_f_recent_worklogs,
        collect_signal_i_project_claude_md,
    )
except Exception as _import_exc:  # noqa: BLE001 — fail-open 优先于一切
    if __name__ != "__main__":
        raise
    print(f"[vault-loader] session_start_load 模块加载失败：{_import_exc}", file=sys.stderr)
    sys.exit(0)

# 诊断模块单独兜底：它是**增强**，不是召回的前提。放进上面那个 try 的话，
# 它加载失败会让整轮召回一起没了；这里退化成「没有诊断」，召回照常。
try:
    from scripts._diagnostics import (
        cache_broken, config_corrupt, notify, take_user_visible,
        vault_path_mismatch, vault_unreachable,
    )
except Exception as _diag_exc:  # noqa: BLE001
    print(f"[vault-loader] 诊断模块不可用，本轮无诊断：{_diag_exc}", file=sys.stderr)

    def notify(*_a, **_k) -> None:  # type: ignore[misc]
        return None

    def take_user_visible(*_a, **_k) -> str:  # type: ignore[misc]
        return ""

    def config_corrupt(*_a, **_k):  # type: ignore[misc]
        return None

    def vault_unreachable(*_a, **_k):  # type: ignore[misc]
        return None

    def cache_broken(*_a, **_k):  # type: ignore[misc]
        return None

    def vault_path_mismatch(*_a, **_k):  # type: ignore[misc]
        return None


# 摘要清单里单个标题的字符上限。标题来自 cache 的 path 键（不可信），除净化外还需
# 截断——否则一个超长标题就能把 systemMessage 撑满、把真实信息挤出视野。
MAX_TITLE_CHARS = 120


def _is_runtime_disabled(home: Path) -> bool:
    if os.environ.get("VAULT_LOADER_DISABLE") == "1":
        return True
    if (home / ".claude" / ".vault-loader-disabled").exists():
        return True
    return False


def _is_opt_out_path(cwd: Path, opt_out: list[str]) -> bool:
    """cwd 是否落在用户配置的 opt-out 目录内——这是用户唯一的「此目录别注入」开关。

    必须归一化 + 边界锚定。裸 `str().startswith()` 有两类错，且都静默：

    1. **漏判（隐私泄露）**：JSON 里写 Windows 路径要双写反斜杠，用户自然写成
       `"D:/work/secret"`，与 cwd 的 `D:\\work\\secret` 前缀比对不上；大小写不同、
       末尾多一个分隔符同样落空。用户以为关掉了，Vault 内容照旧进模型上下文、无任何提示。
    2. **误判（功能受损）**：前缀 `.../secret` 会把兄弟目录 `.../secret-public` 一并拦掉。

    与 `_vault_init._same_path` 用同一套归一（`expanduser().resolve()` + `normcase`），
    所以 symlink / junction 指向同一目录的两种写法也等价。

    异常一律判「不在 opt-out 内」：畸形配置项逐项跳过、不牵连其他项；cwd 自身无法解析
    时返回 False 与 hook 的 fail-open 一致（闸门自身出错不该静默停掉整个 loader）。

    **两个 hook 入口各有一份逐字相同的实现**，由 `tests/test_opt_out_paths.py`
    钉住一致性——改一处必须同步另一处。
    """
    try:
        cwd_n = os.path.normcase(str(Path(cwd).expanduser().resolve()))
    except (OSError, ValueError, RuntimeError):
        return False
    for prefix in opt_out:
        if not isinstance(prefix, str) or not prefix.strip():
            continue
        try:
            p_n = os.path.normcase(str(Path(prefix).expanduser().resolve()))
        except (OSError, ValueError, RuntimeError):
            continue
        # 边界锚定：相等，或落在其下一级——不能用裸前缀比对
        if cwd_n == p_n or cwd_n.startswith(p_n.rstrip(os.sep) + os.sep):
            return True
    return False


def _get_git_toplevel(cwd: Path) -> Path | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1.0,
        )
        if r.returncode == 0:
            return Path(r.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def build_injection_text_ss(cwd, git_top, target_tags, project_notes,
                            top_worklogs, recent_commits, recent_worklog_days):
    """组装 SessionStart 注入正文（= 今日非-dry-run stdout 正文，不含尾 \\n）。"""
    lines = ["📚 知识库（vault-loader）· 项目固定上下文", ""]
    lines.append(f"当前 cwd: {cwd}")
    if git_top:
        lines.append(f"项目: {git_top.name}")
    if target_tags:
        lines.append(f"目标 tag: {', '.join(sorted(target_tags))}")
    lines.append("")
    if project_notes:
        lines.append(f"## 项目相关笔记（近期 {len(project_notes)} 篇）")
        lines.append("")
        for e in project_notes:
            summary = sanitize_injected_text(e.summary or "(无摘要)", keep_newlines=False)
            updated_clean = sanitize_injected_text(e.updated, keep_newlines=False) if e.updated else ""
            mtime_str = f", {updated_clean}" if updated_clean else ""
            # F5：path 与 summary 一样源自不可信笔记 frontmatter，wikilink 嵌入点净化控制字符
            path_clean = sanitize_injected_text(e.path, keep_newlines=False)
            lines.append(f"- [[{path_clean}]] — {summary}{mtime_str}")
        lines.append("")
    if top_worklogs:
        lines.append(f"## 近 {recent_worklog_days} 天工作日志")
        lines.append("")
        for wl in top_worklogs:
            wl_clean = sanitize_injected_text(wl, keep_newlines=False)
            lines.append(f"- [[{wl_clean}]]")
        lines.append("")
    if recent_commits:
        lines.append(f"## 近期提交（{len(recent_commits)}）")
        lines.append("")
        for c in recent_commits:
            lines.append(f"- {c}")
        lines.append("")
    lines.append("💡 关键词相关笔记会在你提问时按需加载；/vault <关键词> 手动展开")
    lines.append("")
    lines.append("⚠️ 以上为知识库历史沉淀，不构成当前代码事实。引用前请按事实优先原则核验。")
    return INJECTION_NOTICE + "\n".join(lines)


def build_summary_ss(project_notes, top_worklogs, recent_commits,
                     project_paths, target_tags, injection_text, display_cfg):
    """SessionStart 用户可见清单摘要（systemMessage）。verbosity=off → None。"""
    verbosity = display_cfg.get("verbosity", "compact")
    if verbosity == "off":
        return None
    show_size = display_cfg.get("show_size", True)
    size = f" · {approx_size_str(injection_text)}" if show_size else ""
    n, m, k = len(project_notes), len(top_worklogs), len(recent_commits)

    def _title(path):
        """标题源自 cache 的 path 键 —— **不可信外部输入**，必须净化后截断（SEC-1）。

        systemMessage 是用户可见的终端 UI。path 里嵌换行即可在其中伪造多行内容：
        实测可完整冒充一条 Claude Code 安全告警并诱导用户执行 `eval $(curl …)`，
        因为伪造文本紧跟在真实的 `📚 vault-loader(启动):` 前缀之后、观感一致。

        `keep_newlines=False` 同时处理 C0 控制字符与 U+2028/2029/0085 这类
        Unicode 行分隔符；截断另防超长标题刷屏。正文侧（build_injection_text_ss）
        本就在净化 path 与 summary，此前唯独摘要侧的标题漏了。
        """
        last = path.split("/")[-1]
        last = last[:-3] if last.endswith(".md") else last
        return sanitize_injected_text(last, keep_newlines=False)[:MAX_TITLE_CHARS]

    def _why(e):
        if e.path in project_paths:
            return "项目目录"
        if target_tags & set(e.tags):
            return "标签"
        return "?"

    if verbosity == "compact":
        titles = "·".join(_title(e.path) for e in project_notes[:3])
        more = "…" if n > 3 else ""
        return (f"📚 vault-loader(启动): {n}笔记[{titles}{more}] "
                f"{m}日志 {k}提交{size} · /vault 展开")
    head = f"📚 vault-loader · 启动注入 · {n} 笔记 / {m} 日志 / {k} 提交{size}"
    body = [f"- {_title(e.path)}  [{_why(e)}]" for e in project_notes]
    tail = f"日志 {m} 篇 · 提交 {k} 条"
    return "\n".join([head, *body, tail, "💡 /vault <关键词> 展开全文"])


def _check_vault_path_mismatch(config: dict, home: Path, config_fell_back: bool) -> None:
    """跨 skill vault 路径一致性自检 → 诊断通道。全程 fail-open。"""
    try:
        pair = compare_vault_paths(config, home)
        if pair:
            notify(vault_path_mismatch(pair[0], pair[1], config_fell_back=config_fell_back))
    except Exception as exc:  # noqa: BLE001
        print(f"[vault-loader] vault 路径自检失败，已跳过：{exc}", file=sys.stderr)


def _finish(
    config: dict,
    cwd: Path,
    additional_context: str | None = None,
    system_message: str | None = None,
) -> int:
    """本 hook 的**唯一**出口：把缓冲里的诊断与本轮正常输出合并成一次 emit。

    见 prompt_submit_load._finish 的同款说明——`emit` 写的是裸 JSON、无分隔符，
    一次执行调用两次就是两段拼接文档，Claude Code 侧解析失败后会把整个原始 stdout
    当 plainText 推进模型上下文。所以诊断绝不能自己 emit。
    """
    try:
        diag_text = take_user_visible(config, cwd)
    except Exception as exc:  # noqa: BLE001 — 诊断绝不阻断召回
        print(f"[vault-loader] 诊断渲染失败，已跳过：{exc}", file=sys.stderr)
        diag_text = ""
    if diag_text:
        system_message = f"{diag_text}\n{system_message}" if system_message else diag_text
    emit(additional_context, system_message, "SessionStart")
    return 0


def main() -> int:
    home = Path.home()

    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        hook_input = {}

    cwd = Path(hook_input.get("cwd", os.getcwd()))

    if _is_runtime_disabled(home):
        return 0

    config, cfg_fallback, cfg_detail = load_config_ex()
    if not config.get("enabled", True):
        return 0
    if not config.get("session_start", {}).get("enabled", True):
        return 0
    if _is_opt_out_path(cwd, config.get("opt_out_paths", [])):
        return 0

    # 信号 I：项目 CLAUDE.md（disable / tags / extra_paths）。用 git 根作项目标识。
    # 提前到 vault 检查之前：disable 是停用闸门，而**所有**停用途径都必须先于诊断生效，
    # 否则明确关停了 vault-loader 的项目仍会收到诊断。代价是 vault 不可达时多跑一次
    # git rev-parse；SessionStart 每会话一次、非热路径，可接受。
    git_top = _get_git_toplevel(cwd)
    project_root = git_top or cwd
    i_result = collect_signal_i_project_claude_md(project_root)
    if i_result.disabled:
        return 0

    # ↓↓↓ 停用闸门到此为止，从这里开始才允许登记诊断 ↓↓↓
    if cfg_fallback == "corrupt":
        notify(config_corrupt(cfg_detail))

    # 启动自检：vault 路径跨 skill 一致性。原先在 load_config 之后立即执行——那时它
    # 只写 stderr 所以无人察觉，但位置其实早于 enabled 闸门，改走用户可见通道后
    # 会击穿「永久停用」逃生阀。故一并下移到这里。
    _check_vault_path_mismatch(config, home, config_fell_back=cfg_fallback is not None)

    vault_path = Path(config["vault_path"]).expanduser()
    # 零配置：非 dry-run 且 vault_path 仍是默认值时才自动建目录（幂等；失败由顶层
    # fail-open 兜底）。用户**显式配置**过的路径不存在时不代建——见 ensure_vault_if_default
    # 的不变量说明，直接落到下面的「vault 路径不可达」早退。
    #
    # config 回退时额外抑制：回退后 vault_path 恰好**就是**默认值，代建会把那个错误路径
    # 连同 .meta/ 建出来，现场看起来像一次正常新装，真实 vault 完全没被读。
    if not config.get("dry_run", False) and cfg_fallback is None:
        try:
            ensure_vault_if_default(vault_path)
        except Exception:
            pass
    if not vault_path.exists():
        notify(vault_unreachable(vault_path))
        return _finish(config, cwd)

    # 目标 tag 集（信号 B ∪ I）
    target_tags = set(i_result.tags) | collect_signal_b_keyword_map(
        cwd, config.get("keyword_to_tags", {})
    )

    ss_cfg = config["session_start"]

    # 信号 A：项目目录笔记（按 git 根 basename，feasibility F-6）
    project_paths = collect_signal_a_project_dir(
        project_root, vault_path, i_result.extra_paths
    )

    # 信号 F：近期工作日志（按 git 根 basename 作项目标识）
    worklog_result = collect_signal_f_recent_worklogs(
        project_root, vault_path, ss_cfg["recent_worklog_days"]
    )

    # 近期 git 提交（原始 oneline 展示）。传 project_root 与 A/F 一致（git 自动定位仓库根）。
    recent_commits = collect_recent_commits(project_root, ss_cfg.get("max_commits", 5))

    # 项目相关笔记：确定性成员（项目目录 ∪ 标签匹配），按 mtime 倒序，无打分。
    # 注意：cache 为空不早退——工作日志/提交不依赖 cache，仍应渲染。
    entries, cache_status = load_cache_status(vault_path)
    # OBS-4：只要仓库里有一条 commit，横幅就恒定出现，四种 cache 状态下逐字节相同。
    # 用户看到熟悉横幅、「0 笔记」被读成「这个项目还没有笔记」——而实际是索引已死。
    # 这比完全静默更糟：它主动提供了错误的健康证据。
    # 但只有真损坏才报——ABSENT/VERSION_MISMATCH/EMPTY 都是健康态，对它们告警会命中
    # 每个新装用户，以及下次 CACHE_VERSION bump 后的全部存量用户。
    if cache_status.is_failure():
        notify(cache_broken(cache_status.value, vault_path))
    include_tag = ss_cfg.get("include_tag_matched_notes", True)
    exclude_tags = {t.lower() for t in config.get("relevance", {}).get("exclude_note_tags", [])}

    def _is_project_note(entry) -> bool:
        # 第1层：archived 等排除 tag 不进召回池（卫生措施非安全边界；/vault 手动检索不受影响）
        if is_archived(entry, exclude_tags):
            return False
        # 项目目录直接命中（强信号），或标签匹配（弱信号，开关可关）
        if entry.path in project_paths:
            return True
        return include_tag and bool(target_tags & set(entry.tags))

    project_notes = [e for e in entries.values() if _is_project_note(e)]
    project_notes.sort(key=lambda e: -e.mtime)
    project_notes = project_notes[: ss_cfg["max_notes"]]

    top_worklogs = worklog_result.paths[: ss_cfg["max_recent_worklogs"]]

    # 三组全空才静默
    if not project_notes and not top_worklogs and not recent_commits:
        if config.get("verbose_on_skip"):
            return _finish(
                config, cwd,
                system_message="📚 vault-loader: 0 候选（当前 cwd 无可关联的笔记/日志/提交）",
            )
        return _finish(config, cwd)

    # 渲染 + 输出
    dry_run = config.get("dry_run", False)
    display_cfg = config.get("display", {})
    user_visible = display_cfg.get("user_visible", True)
    injection_text = build_injection_text_ss(
        cwd, git_top, target_tags, project_notes,
        top_worklogs, recent_commits, ss_cfg["recent_worklog_days"],
    )
    summary = (build_summary_ss(project_notes, top_worklogs, recent_commits,
                                project_paths, target_tags, injection_text, display_cfg)
               if user_visible else None)

    if dry_run:
        # dry-run 下诊断并入同一条 systemMessage（由 _finish 置顶），不另起一次 emit。
        return _finish(
            config, cwd,
            system_message=(f"[DRY-RUN] 本应注入：\n{summary}" if summary else None),
        )

    # 更新 state（项目相关笔记 + 工作日志路径），供 UserPromptSubmit(J) 去重。
    # 顺序：state 先写、emit 后发——_finish 会调 save_diag_ts，与 save_injected 写同一个
    # state 文件，两者都是读-改-写，反序会让先写的那次被覆盖。
    try:
        from scripts._state import save_injected
        save_injected(cwd, [e.path for e in project_notes] + top_worklogs)
    except Exception as exc:
        print(f"[vault-loader] state 写入失败：{exc}", file=sys.stderr)

    return _finish(config, cwd, additional_context=injection_text, system_message=summary)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[vault-loader] session_start_load 崩溃：{exc}", file=sys.stderr)
        sys.exit(0)  # 永不破坏会话
