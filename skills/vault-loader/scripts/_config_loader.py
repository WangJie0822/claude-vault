"""加载 vault-loader 配置。缺失自动写默认，损坏保留原文件回退默认。"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

DEFAULT_CONFIG: dict = {
    "enabled": True,
    "dry_run": False,
    "vault_path": str(Path.home() / ".claude" / "knowledge-vault"),

    "session_start": {
        "enabled": True,
        "max_notes": 5,
        "max_recent_worklogs": 3,
        "recent_worklog_days": 7,
        "max_commits": 5,
        "include_tag_matched_notes": True,
        # 注：min_score 已废弃（B'' startup 不再打分）。旧 config 若仍带该键，
        # 经 _deep_merge 容错保留、session_start 静默忽略，不写进新生成配置。
    },

    "user_prompt_submit": {
        "enabled": True,
        "max_notes": 3,
        # min_score / fulltext_threshold 已废弃：UPS 闸门与全文触发改用 relevance 段的
        # min_topical_score / fulltext_topical_threshold；保留仅向后兼容旧 config，运行时不读。
        "min_score": 5,
        "fulltext_threshold": 10,
        "fulltext_max_bytes": 8192,
        "min_keyword_count": 2,
        "state_ttl_hours": 24,
    },

    "scoring": {
        "exact_project_dir": 5,
        "tag_target_set_hit": 3,
        "commit_keyword_hit": 2,
        "commit_keyword_cap": 6,
        "worklog_cooccur": 2,
        "mtime_recent_30d": 1,
        "mtime_recent_90d": 0.5,
        "prompt_tag_hit": 4,
        "prompt_summary_hit": 2,
    },

    "keyword_to_tags": {},

    "opt_out_paths": [
        "/tmp",
        "/private/tmp",
        str(Path.home() / "AppData" / "Local" / "Temp"),
    ],

    "verbose_on_skip": False,

    "display": {
        "user_visible": True,
        "verbosity": "list",  # Phase 0 P3 已验证多行渲染可用（2026-06-22）
        "show_size": True,
    },

    "relevance": {
        "strip_slash_command": True,        # 剥 prompt 首个 slash 命令名 token
        "min_topical_score": 4,             # 精度闸门：仅 topical_score ≥ 此值才注入
        # fulltext_topical_threshold 与 confidence_bands.high 同值=6 时：topical=6 的条目，若由
        # ≥2 个不同关键词命中则走全文分支；若仅单个词刷满（B 纵深防御 _FULLTEXT_MIN_DISTINCT），
        # 则被挡回清单且标"中置信"。故清单内常态只出现"中置信"——既因残留条目 topical 多为 4，也因
        # 单词刷满的 topical=6 被 dist 闸门降级。单独调高本阈值且有 ≥2 词佐证才会让清单出现"高置信"。
        "fulltext_topical_threshold": 6,    # 强命中自动加载全文的 topical 阈值（最强档之一，另需 dist≥2）
        "confidence_bands": {"high": 6},    # topical ≥ high 且 dist≥2 → 高置信，否则中置信
        "short_summary_chars": 20,          # summary 短于此回退文件名标题
        # 后续优化（2026-06-23）：英文 token 切分 + 兜底提示
        "split_english_token": True,        # 英文 token 按 [_-] 再切分（治路径碎片黏连）
        "en_subtoken_min": 4,               # 子片最小长度；3 经实证为召回灾难（bug→146 tag），默认 4
        "fallback_hint": True,              # topical 全失配（仅触发点2）时一行用户可见提示
        # 拦截非用户手输 prompt（后台 task-notification/系统注入）——含 UUID/tool-id/路径碎片污染
        "skip_non_user_prompts": True,
    },
}


def _deep_merge(default: dict, override: dict) -> dict:
    """递归合并：override 优先，dict 类型字段做深度合并。"""
    result = deepcopy(default)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_config(path: Path | None = None) -> dict:
    """加载配置。
    - 缺失：写默认值到 path，返回默认值
    - 损坏：保留原文件，stderr 警告，返回默认值
    - 正常：与默认值深合并
    """
    if path is None:
        path = Path.home() / ".claude" / "skills" / "vault-loader" / "config.json"

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        return deepcopy(DEFAULT_CONFIG)

    try:
        text = path.read_text(encoding="utf-8")
        override = json.loads(text)
        if not isinstance(override, dict):
            raise ValueError("config root 必须为 object")
        return _deep_merge(DEFAULT_CONFIG, override)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"[vault-loader] config 损坏，回退默认值：{exc}", file=sys.stderr)
        return deepcopy(DEFAULT_CONFIG)


def check_vault_path_consistency(vl_config: dict, home: Path | None = None) -> None:
    """启动自检：若 summarize-session config 可读且 vault 路径不一致，打印 stderr 告警。

    完全 fail-open：任何异常静默吞掉，绝不抛出、绝不影响调用方正常流程。
    不引入硬性跨 skill 导入依赖——仅 best-effort 读 JSON 文件。
    """
    try:
        if home is None:
            home = Path.home()
        ss_cfg_path = home / ".claude" / "skills" / "summarize-session" / "config.json"
        if not ss_cfg_path.exists():
            return  # 未配置 summarize-session，静默跳过
        try:
            ss_raw = json.loads(ss_cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # 读取或解析失败，静默跳过
        if not isinstance(ss_raw, dict):
            return
        ss_vault_str = ss_raw.get("default_vault_path")
        if not ss_vault_str:
            return  # 字段缺失或空值，无法比较
        # 解析两侧路径（expanduser + resolve，忽略符号链接差异）
        try:
            vl_resolved = Path(vl_config.get("vault_path", "")).expanduser().resolve()
            ss_resolved = Path(ss_vault_str).expanduser().resolve()
        except (OSError, ValueError):
            return
        if vl_resolved != ss_resolved:
            print(
                f"[vault-loader] 警告：vault 路径不一致——"
                f"vault-loader.vault_path={vl_resolved} vs "
                f"summarize-session.default_vault_path={ss_resolved}；"
                f"请运行 /summarize-session --set-default 或手动对齐。",
                file=sys.stderr,
            )
    except Exception:  # noqa: BLE001 — fail-open，静默吞掉一切异常
        pass
