"""写端补三个字段：两个承重阈值 + 全量成因计数。

背景（2026-09-01 两路评审，均已独立复算）：

- **成因分布口径造假**：报表那行「near-miss 成因分布」的 docstring 声称「刻意保持
  全量口径，它回答『excluded 都因为什么落榜』」，实际算在 `near_miss` 上——而后者
  是**按 topical 降序取 top-10 的截断样本**，实测采样率 1.38%（11,010 / 795,383）、
  长度分布 `{10: 1101}` 即 100% 的轮次都在截断。偏斜方向是结构性的：dedup 条目
  不看 topical 就 excluded（可达 11），新篇落 excluded 的条件却是 `t < min_topical`
  （必 < 4），于是按 topical 降序时 dedup 天然占满槽位。后果是报表给出 dedup 占比
  45%，而由 state 文件独立定界的真实量级约 1%。
- **阈值不落盘**：`min_topical_score` 与 `fulltext_topical_threshold` 是两个最承重的
  判据，样本期内 `fulltext_topical_threshold` 已由 6 改成 10（实测两期全文注入率
  65.4% vs 40.1%，而报表给出合并值 45.9%，不描述任何一个时期）。本项目已经为
  `max_notes` 踩过同一个坑并补上，理由逐字适用于这两个字段，却漏了它们。
"""

from pathlib import Path

from scripts import _metrics


def _entry(path, topical, dedup="", admitted=False):
    from scripts._decision import EntryDecision
    return EntryDecision(path=path, topical=topical, total=0.0, hits=[],
                         admitted=admitted, admit_arm="", dedup=dedup)


def _decision(excluded, admitted=None):
    from scripts._decision import Decision
    return Decision(admitted=admitted or [], excluded=excluded,
                    fulltext_path=None, fulltext_arm="",
                    any_relevant=True, relaxed=False, gate_reason="")


def _build(d, tmp_path, **kw):
    return _metrics.build_record(d, {"内存", "泄露", "重启"}, Path("D:/proj"),
                                 session_id="s1", prompt_id="p1",
                                 salt=_metrics.get_salt(tmp_path), src="", **kw)


def test_thresholds_are_persisted(tmp_path):
    """两个承重阈值随记录落盘 —— analyze_metrics 不读 config。

    变异验证：删掉 build_record 里 min_topical/ft_topical 的落盘，本用例转红。
    """
    r = _build(_decision([]), tmp_path, min_topical=4.0, ft_topical=10.0)
    assert r["min_topical"] == 4.0
    assert r["ft_topical"] == 10.0


def test_thresholds_absent_when_not_supplied(tmp_path):
    """不传时不落该键 —— 与「旧记录没有这个字段」保持同一种形态，
    读端才能用 `in` 判存在，而不是把「没记录」误读成某个具体值。"""
    r = _build(_decision([]), tmp_path)
    assert "min_topical" not in r
    assert "ft_topical" not in r


# ── 全量成因计数：H-2 的正解 ────────────────────────────────────────────

def test_dedup_counts_covers_all_excluded_not_the_truncated_sample(tmp_path):
    """`dedup_counts` 必须统计**全部** excluded，而不是 near_miss 那 10 条。

    构造刻意复刻真实数据的偏斜形态：dedup 条目 topical 高（不看分就被排除）、
    新篇 topical 低（因为没过精度闸门才被排除）。按 topical 降序取 top-10 时
    dedup 会占满窗口，于是「窗口内占比」远高于真实占比。

    变异验证：把 dedup_counts 改成遍历 near_miss 样本，本用例转红。
    """
    excluded = ([_entry(f"hi/{i}.md", 11.0, dedup="fulltext_injected")
                 for i in range(10)]
                + [_entry(f"lo/{i}.md", 2.0, dedup="") for i in range(90)])
    r = _build(_decision(excluded), tmp_path, near_miss_k=10)

    # near_miss 窗口被 dedup 占满 —— 这正是报表此前看到的偏斜视图
    assert len(r["near_miss"]) == 10
    assert all(e["dedup"] == "fulltext_injected" for e in r["near_miss"]), \
        "构造前提不成立：窗口应被高 topical 的 dedup 条目占满"

    # 全量计数必须反映真实构成：10 个 dedup + 90 个新篇
    assert r["dedup_counts"] == {"fulltext_injected": 10, "": 90}, \
        f"dedup_counts 不是全量口径，实际 {r['dedup_counts']}"


def test_dedup_counts_sums_to_n_excluded(tmp_path):
    """全集守恒：分档之和必须等于 n_excluded。

    这条断言不依赖「我记得有哪些 dedup 取值」——将来新增一种成因而忘了登记时，
    分母对不上会立刻转红。本仓库吃过「分档统计漏掉一半档位、加一条 high 等于
    没加」的亏，守恒断言是那类缺陷唯一可靠的探针。

    变异验证：让 dedup_counts 只统计非空 dedup（漏掉 "" 那档），本用例转红。
    """
    excluded = ([_entry(f"a/{i}.md", 9.0, dedup="fulltext_injected") for i in range(3)]
                + [_entry(f"b/{i}.md", 8.0, dedup="candidate_injected") for i in range(5)]
                + [_entry(f"c/{i}.md", 1.0, dedup="") for i in range(7)])
    r = _build(_decision(excluded), tmp_path)
    assert sum(r["dedup_counts"].values()) == r["n_excluded"] == 15


def test_dedup_counts_empty_when_no_excluded(tmp_path):
    r = _build(_decision([]), tmp_path)
    assert r["dedup_counts"] == {}
    assert r["n_excluded"] == 0


# ── 端到端：字段必须真的到达磁盘 ──────────────────────────────────────────

def test_thresholds_and_counts_reach_disk_end_to_end(
        tmp_home, tmp_vault, write_frontmatter_cache):
    """真实 hook 跑一轮，落盘记录必须含两个阈值与全量成因计数。

    这条不能用 build_record 的单测替代：写端落了字段而**调用方漏传**时，
    上面那几条照样全绿，而生产数据里这两个键永远不存在——本仓库的 `src`
    字段正是这么空了一整个版本的。

    变异验证：删掉调用点的 min_topical=/ft_topical= 两行，本用例转红。
    """
    import json as _json
    from tests.test_metrics_optout import _setup_vault_and_cfg, _run

    work = tmp_home.parent / "work-thresholds"
    work.mkdir(parents=True, exist_ok=True)
    cfg = _setup_vault_and_cfg(tmp_vault, write_frontmatter_cache, {})
    r = _run(tmp_home, work, cfg)
    assert r.returncode == 0, r.stderr

    files = list((tmp_home / ".claude" / "vault-loader-metrics").rglob("*.jsonl"))
    assert files, "前提不成立：本轮没有任何 metrics 落盘，后面的断言无意义"
    recs = [_json.loads(l) for p in files
            for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    scored = [x for x in recs if not x.get("gate")]
    assert scored, "前提不成立：没有走到打分的记录"

    rec = scored[0]
    assert "min_topical" in rec, "调用方没把 min_topical 传下去"
    assert "ft_topical" in rec, "调用方没把 ft_topical 传下去"
    assert "dedup_counts" in rec
    assert sum(rec["dedup_counts"].values()) == rec["n_excluded"],         "落盘的全量成因计数与 n_excluded 对不上"


# ── F2：用户实际看到哪几篇，直接落盘而非读端重建 ────────────────────────

def test_shown_is_annotatable():
    """`shown` 必须在 annotate 允许集里，否则回填会被静默丢弃。"""
    from scripts._metrics import _ANNOTATE_ALLOWED
    assert "shown" in _ANNOTATE_ALLOWED


def test_shown_reaches_disk_and_matches_rendered(tmp_home, tmp_vault,
                                                 write_frontmatter_cache):
    """端到端：落盘的 shown 必须与渲染层实际输出的篇目一致。

    读端此前靠 `admitted` 数组顺序重建，而实测 97.7% 的轮次前 5 条 total 并列、
    并列次序由不落盘的 mtime 决定 —— 重建今天对只是巧合于稳定排序。

    变异验证：删掉调用点的 annotate(shown=...)，本用例转红。
    """
    import json as _json
    from tests.test_metrics_optout import _setup_vault_and_cfg, _run

    work = tmp_home.parent / "work-shown"
    work.mkdir(parents=True, exist_ok=True)
    cfg = _setup_vault_and_cfg(tmp_vault, write_frontmatter_cache, {})
    r = _run(tmp_home, work, cfg)
    assert r.returncode == 0, r.stderr

    files = list((tmp_home / ".claude" / "vault-loader-metrics").rglob("*.jsonl"))
    assert files, "前提不成立：没有落盘"
    recs = [_json.loads(l) for p in files
            for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    scored = [x for x in recs if not x.get("gate") and x.get("n_admitted")]
    assert scored, "前提不成立：没有产生注入的轮次"

    rec = scored[0]
    assert "shown" in rec, "调用方没有回填 shown"
    assert isinstance(rec["shown"], list) and rec["shown"], "shown 为空"
    mn = rec.get("max_notes", 3)
    assert len(rec["shown"]) <= mn, (
        f"shown 超过 max_notes：{len(rec['shown'])} > {mn}")
    paths = {a.get("path") for a in rec.get("admitted", []) if isinstance(a, dict)}
    assert set(rec["shown"]) <= paths | {(rec.get("ft") or {}).get("path")}, \
        "shown 里出现了不在 admitted/ft 中的路径"
