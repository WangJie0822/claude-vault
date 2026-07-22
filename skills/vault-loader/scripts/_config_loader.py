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
        "prompt_keyword_hit": 5,   # 3→5：keywords 是策展的精确召回信号，
                                   # 必须能胜过泛 tag 命中（实测 keywords=3 时打不过 tag=4）
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
        "use_keywords": True,               # 止血开关：false 时 scorer 忽略 keywords，不杀整个 loader
        "min_topical_score": 4,             # 精度闸门：仅 topical_score ≥ 此值才注入
        # fulltext_topical_threshold 与 confidence_bands.high 同值=6 时：topical=6 的条目，若命中词
        # 达强证据档（evidence chain，见 _scorer.has_strong_evidence）则走全文分支；若仅单链弱证据
        # （如单个词刷满/单一 bigram 链），则被挡回清单且标"中置信"。故清单内常态只出现"中置信"——
        # 既因残留条目 topical 多为 4，也因弱证据的 topical=6 被强证据档降级。单独调高本阈值且有
        # 强证据佐证才会让清单出现"高置信"。
        "fulltext_topical_threshold": 6,    # 强命中自动加载全文的 topical 阈值（最强档之一，另需强证据档）
        "confidence_bands": {"high": 6},    # topical ≥ high 且强证据档 → 高置信，否则中置信
        "short_summary_chars": 20,          # summary 短于此回退文件名标题
        # 后续优化（2026-06-23）：英文 token 切分 + 兜底提示
        "split_english_token": True,        # 英文 token 按 [_-] 再切分（治路径碎片黏连）
        "en_subtoken_min": 4,               # 子片最小长度；3 经实证为召回灾难（bug→146 tag），默认 4
        "fallback_hint": True,              # topical 全失配（仅触发点2）时一行用户可见提示
        # 拦截非用户手输 prompt（后台 task-notification/系统注入）——含 UUID/tool-id/路径碎片污染
        "skip_non_user_prompts": True,
        # PERF-P2：prompt 关键词数（M）软上限。巨型 prompt（大段粘贴）会令 O(N×M×K) 评分破
        # <300ms 预算；超上限时取确定性子集（sorted 前 N）。0/None 表示不限。
        "max_prompt_keywords": 30,
        # 第0层（2026-07-02 spec）：CJK bigram 分词与纯 CJK 闸门放宽
        "split_cjk_bigram": True,           # bigram 分词主开关（false=旧 mega-token 行为）
        "relax_pure_cjk_single": True,      # 纯 CJK 单 token 放行触发点1（配合 relaxed 静默）
        # 第1层：召回池排除 tag（UPS+SessionStart 共用；[]=关闭；/vault 手动检索不受影响）
        "exclude_note_tags": ["archived"],
        # tag-IDF：泛 tag（superpowers 覆盖 142/680）与 singleton tag 此前等权，
        # 是噪声与漏召回同时发生的直接成因。use_tag_idf=false 为止血开关，
        # 关闭后数值等价回到旧行为，不杀整个 loader。
        "use_tag_idf": True,
        "tag_idf_floor": 0.5,       # 泛 tag 的保底因子；0 会让泛 tag 归零、行为剧变
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


def _normalize_relevance(rel: dict) -> None:
    """归一化 relevance 段易错键（deep-merge 无类型校验；防单个笔误配置静默关停召回）。"""
    ex = rel.get("exclude_note_tags")
    if isinstance(ex, list):
        rel["exclude_note_tags"] = [t for t in ex if isinstance(t, str)]
    else:
        rel["exclude_note_tags"] = ["archived"]
    rel["split_cjk_bigram"] = bool(rel.get("split_cjk_bigram", True))
    rel["relax_pure_cjk_single"] = bool(rel.get("relax_pure_cjk_single", True))
    try:
        mk = rel.get("max_prompt_keywords", 30)
        # max(0, ...) 同时归一「域」：负值（误配）会让下游头尾切片 hn/tn 转负→退化截断，
        # 与本函数「防笔误静默劣化召回」初衷相悖，故 clamp 到非负（0 表示不截断上限）。
        rel["max_prompt_keywords"] = max(0, int(mk)) if mk is not None else 0
    except (TypeError, ValueError):
        rel["max_prompt_keywords"] = 30


def _ensure_relevance_normalized(result: dict) -> dict:
    """FIX-4：relevance 段类型兜底——deep-merge 无类型校验，用户误将 relevance 整体配成
    非 dict（如 "relevance":"archived"）会覆盖成非 dict，_normalize_relevance 的 .get 调用
    会抛 AttributeError、逃逸 load_config 的 except（仅捕 JSONDecodeError/ValueError/OSError）。
    非 dict 时先回退 DEFAULT_CONFIG 的 relevance 段再归一化，保证 load_config 永不因此抛异常。"""
    rel = result.get("relevance")
    if not isinstance(rel, dict):
        rel = deepcopy(DEFAULT_CONFIG["relevance"])
        result["relevance"] = rel
    _normalize_relevance(rel)
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
        result = deepcopy(DEFAULT_CONFIG)
        _ensure_relevance_normalized(result)
        return result

    try:
        text = path.read_text(encoding="utf-8")
        override = json.loads(text)
        if not isinstance(override, dict):
            raise ValueError("config root 必须为 object")
        result = _deep_merge(DEFAULT_CONFIG, override)
        _ensure_relevance_normalized(result)
        return result
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"[vault-loader] config 损坏，回退默认值：{exc}", file=sys.stderr)
        result = deepcopy(DEFAULT_CONFIG)
        _ensure_relevance_normalized(result)
        return result


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
