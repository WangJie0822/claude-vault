"""宿主召回默认值单点；读取默认值不必加载模型选择与校验实现。"""

# 仅这些宿主覆盖会流入共用召回核心；不接受任意 config 键。
CODEX_RELEVANCE_DEFAULTS = {
    "fine_rank": False, "fine_supplement": False,
    "coarse_inject": True, "auto_fulltext": False,
    "fine_pin_max": 1, "fine_max_notes": 3, "fine_timeout_sec": 120,
    "fine_max_calls": 20, "fine_session_max_bytes": 1000000,
    "fine_catalog": True, "fine_catalog_max_chars": 80000,
    "fine_recent_prompts": 3, "fine_exclude_tags": ["private"],
    "session_topic_top_n": 30, "session_topic_project_top_k": 20,
    "session_topic_pool_max": 50,
}
CODEX_UPS_DEFAULTS = {"max_notes": 3}
