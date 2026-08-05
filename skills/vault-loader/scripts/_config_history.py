"""历史默认值表：曾经的 DEFAULT_CONFIG 默认值，供 `migrate_config.py` 判断
「首跑物化残留」（spec §8.2）。

背景：Task 6（commit 42f3fc8）已停止首跑全量物化——新装用户 config.json 只写最小占位
`_MINIMAL_STUB`，未来默认值演进对新用户即时生效。但**存量用户**盘上 config.json 仍可能
是旧版本首跑时物化落盘的全量默认值快照；若某键盘上值恰好等于**当前或历史上任一版本**的
默认值（而非用户显式改过），deep-merge 会把它当"用户覆盖"永久锁死，未来该键的默认值
演进对这个用户静默失效。本表登记每个可收敛键"曾经当过默认值"的完整集合，脚本据此判定
"盘上值 ∈ 该键历史默认集合" → 判残留、可安全删除（删后 deep-merge 自动回落到当前
DEFAULT_CONFIG 的最新默认）。

提取来源（本仓库唯一可见历史）：
    git log -p --follow -- skills/vault-loader/scripts/_config_loader.py
逐提交核对（d3c134b「allowlist 导入」新增该文件 → … → 42f3fc8「停止首跑全量物化」）：
`scoring.prompt_keyword_hit` 是**唯一**曾变更默认值的 allowlist 数值键（e4a7462 首次
引入=3，665cf63 改为=5，见该键的注释）；其余全部键自建库以来数值从未变化。
d3c134b 与孤儿提交 cb3a214（origin/master 干净首版）两处"新增"该文件时，涉及键已是
现在这些初值，仓库内不可见更早的历史。

收敛范围（spec §8.2「仅纯调参键」，与 R11/S-4/S-5 处置一致）：
    ALLOWLIST_PREFIXES 四段前缀下的**非布尔数值（int/float）叶子键**。
    - 布尔开关（如 use_tag_idf/use_keywords/split_cjk_bigram 等止血开关或功能开关）、
      列表（exclude_note_tags）——即使数值上"恰好等于默认"也不参与残留判定，因为
      开关值的语义是"用户是否要这个行为"，历史等值判定法对开关不适用（宁可漏删
      存量残留，也不可误删用户显式的开关选择）。
    - home 派生键（vault_path/opt_out_paths 等）——逐字比较对其恒失配（每台机器
      home 路径不同），本就不该入表；且已被列入 EXCLUDED_KEYS 兜底。
    - enabled/dry_run 等结构性开关键——任何情况下不删（EXCLUDED_KEYS 按 leaf 名
      在任意路径深度命中即排除，覆盖 session_start.enabled/user_prompt_submit.enabled
      等嵌套出现的场景）。
"""
from __future__ import annotations

ALLOWLIST_PREFIXES = ("scoring.", "relevance.", "session_start.", "user_prompt_submit.")

# 任意路径 segment（不论出现在路径哪一层）命中即排除——这些键即使数值恰好等于
# 历史默认，也永不可被判定/删除（S-4/S-5/R11 处置）。
EXCLUDED_KEYS = {
    "enabled", "dry_run", "vault_path", "opt_out_paths", "display",
    "telemetry", "keyword_to_tags",
}

# 值为「该键曾经当过的默认值集合」（history，非当前唯一默认）；表覆盖 ALLOWLIST
# 前缀下当前 DEFAULT_CONFIG 全部数值叶子键（由 test_current_defaults_registered 守卫）。
HISTORICAL_DEFAULTS: dict[str, list] = {
    # ── session_start.*（首见 d3c134b/cb3a214，此后从未变更） ──────────────
    "session_start.max_notes": [5],
    "session_start.max_recent_worklogs": [3],
    "session_start.recent_worklog_days": [7],
    "session_start.max_commits": [5],

    # ── user_prompt_submit.*（首见 d3c134b/cb3a214，此后从未变更；min_score/
    #    fulltext_threshold 是 _config_loader.py 注释标注的已废弃死键，仍登记以
    #    支持存量 config 里这两个死键残留的清理） ─────────────────────────
    "user_prompt_submit.max_notes": [3],
    "user_prompt_submit.min_score": [5],
    "user_prompt_submit.fulltext_threshold": [10],
    "user_prompt_submit.fulltext_max_bytes": [8192],
    "user_prompt_submit.min_keyword_count": [2],
    "user_prompt_submit.state_ttl_hours": [24],

    # ── scoring.*（首见 d3c134b/cb3a214；仅 prompt_keyword_hit 变过一次） ───
    "scoring.exact_project_dir": [5],
    "scoring.tag_target_set_hit": [3],
    "scoring.commit_keyword_hit": [2],
    "scoring.commit_keyword_cap": [6],
    "scoring.worklog_cooccur": [2],
    "scoring.mtime_recent_30d": [1],
    "scoring.mtime_recent_90d": [0.5],
    "scoring.prompt_tag_hit": [4],
    "scoring.prompt_summary_hit": [2],
    # e4a7462（2026-06-27 "keyword 命中独立权重打分"）首次引入 prompt_keyword_hit=3；
    # 665cf63（2026-07-22 "tag 命中按 IDF 加权+keywords 权重 3→5"）改为 5——
    # "keywords 是策展的精确召回信号，必须能胜过泛 tag 命中（实测 keywords=3 时打不过 tag=4）"。
    "scoring.prompt_keyword_hit": [3, 5],

    # ── relevance.*（数值调参键；布尔开关/列表见模块 docstring，不入本表） ──
    "relevance.min_topical_score": [4],
    "relevance.fulltext_topical_threshold": [6],
    "relevance.confidence_bands.high": [6],
    "relevance.short_summary_chars": [20],
    "relevance.en_subtoken_min": [4],
    # c27a39a（2026-06-28 "Batch B fast-follow"）引入 max_prompt_keywords=30；
    # b6f9cc3/4844455 只改了截断/clamp 的实现代码，未改这个默认值。
    "relevance.max_prompt_keywords": [30],
    # 665cf63（2026-07-22）引入 tag_idf_floor=0.5，此后未变。
    "relevance.tag_idf_floor": [0.5],
}
