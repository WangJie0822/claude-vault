# -*- coding: utf-8 -*-
"""session_topic 是否**真的生效**必须落进 metrics，否则它的失效永远是静默的。

2026-09-09 的教训：提炼子进程与 hook 父进程落在两个 runtime 命名空间，18/18 次
提炼全部读不到（详见 `test_topic_namespace.py` 的 docstring）。整整一周里，
`--report` 没有任何一栏会变、全套用例全绿、注入正文一切正常——因为**决策面记录里
根本没有一个字段跟 session_topic 有关**。修好那次落点不等于修好这件事：下一个成因
（TTL 配错、state 被清、提炼恒失败、闸门顺序变化）会以同样的方式静默。

落 `n_topic_words`（本轮实际读到并参与打分的主题词数）而不是逐条 hit：
- 它是能证伪「功能生效」的**最小充分信号**——恒 0 即失效，一眼可见；
- 逐条 hit 要么改 `_prompt_topical_hits` 的热路径签名，要么在 `build_record` 里
  按同一规则重算一遍。后者等于把判据抄成两份，正是本仓库反复记过的漂移源头。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from scripts import _metrics
from scripts._state import configure_context
from scripts._topic import save_session_topic
from scripts.analyze_metrics import load_records, render_report, summarize

TOPIC_WORDS = ["配额", "fulltext", "注入"]


def _setup(tmp_home: Path, tmp_vault: Path, write_frontmatter_cache, extra: dict) -> None:
    write_frontmatter_cache({
        "技术笔记/quota.md": {
            "tags": ["配额", "fulltext"], "category": "技术笔记",
            "summary": "fulltext 配额的实现与坑", "mtime": 1900000000,
        }
    })
    (tmp_vault / "技术笔记").mkdir()
    (tmp_vault / "技术笔记" / "quota.md").write_text("# quota", encoding="utf-8")
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault),
                               **extra}), encoding="utf-8")


def _run_main_inprocess(monkeypatch, capsys, cwd: Path, prompt: str, session: str):
    """跑一轮真实 UPS，并**拦住提炼 spawn**。

    不拦的话，「state 里没有主题词」那一档满足 spawn 门禁，会真起一条 detached 子进程
    链（`python -m scripts._topic` → `claude -p --model haiku`）。在 `tmp_home` 下它
    必然以 `Not logged in` 失败、**不产生费用**（实测），但仍是非预期的外部进程副作用
    与耗时；而「不花钱」这一点**依赖 tmp_home 隔离这个隐含前提**，一旦有人在未隔离
    HOME 的环境下跑它，前提即破。既有 wiring 用例一律 patch 掉它，这里跟齐。
    """
    import scripts.prompt_submit_load as P
    spawned: list = []
    monkeypatch.setattr(P, "spawn_topic_extraction",
                        lambda *a, **k: (spawned.append(a), True)[1])
    payload = {"cwd": str(cwd), "prompt": prompt,
               "session_id": session, "prompt_id": "pid-T"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    rc = P.main()
    cap = capsys.readouterr()
    return rc, cap.out, cap.err, spawned


def _records(home: Path) -> list[dict]:
    """扫 tmp_home 全树取记录，刻意不经 `metrics_dir()`。

    读端复用写端的路径解析，会让「写读落在不同命名空间」这类缺陷对本用例隐身——
    正是 `test_topic_namespace.py` 那个缺陷的形态。这里只关心「记录有没有落下来」。
    """
    out = []
    for f in home.rglob("*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def test_build_record_carries_topic_word_count(tmp_path: Path) -> None:
    """`build_record` 必须把本轮生效的主题词数原样落盘。"""
    from scripts._decision import Decision

    rec = _metrics.build_record(
        Decision(admitted=[], excluded=[], fulltext_path=None, fulltext_arm="",
                 any_relevant=False, relaxed=False, gate_reason=""),
        ["配额"], tmp_path, session_id="s", prompt_id="p", salt=b"x",
        src="", n_topic_words=3)
    assert rec["n_topic_words"] == 3


@pytest.mark.parametrize("preset,expected", [(TOPIC_WORDS, 3), ([], 0)])
def test_ups_record_reports_effective_topic_words(
        tmp_home: Path, tmp_vault: Path, write_frontmatter_cache,
        monkeypatch, capsys, preset, expected) -> None:
    """端到端接线：走完整 UPS 一轮，落盘记录里的计数必须等于真实读到的词数。

    这条才是承重断言。只测 `build_record` 收到参数会落盘，钉不住**调用点有没有
    把它接上去**——而「定义在、接线断」正是本次缺陷家族的形态。两档参数化让
    「恒 0」与「恒等于常数」两种假实现都活不下来。
    """
    cwd = tmp_home.parent / f"p-topic-{expected}"
    cwd.mkdir()
    session = f"sess-obs-{expected}"
    _setup(tmp_home, tmp_vault, write_frontmatter_cache,
           {"metrics": {"enabled": True}})
    configure_context("claude", session)
    if preset:
        save_session_topic(cwd, session, preset)

    _metrics.reset()
    rc, out, err, spawned = _run_main_inprocess(monkeypatch, capsys, cwd,
                                                "先修 fulltext 配额", session)
    assert rc == 0
    # 钉住「那个 patch 不是多余的」：state 里没有主题词的那一档确实会走到 spawn 门禁。
    # 没有这条，将来有人删掉 patch 也看不出差别，直到某台未隔离 HOME 的机器上真跑起来。
    assert len(spawned) == (0 if preset else 1), (
        f"preset={preset!r} 时 spawn 调用次数应为 {0 if preset else 1}，实际 {len(spawned)}")

    # 判据用 `"admitted" in r` 而不是 `"gate" not in r`：完整记录**也**带 `gate` 键
    # （值为空串 `decision.gate_reason`），后者会把它一并滤掉、断言恒空。
    recs = [r for r in _records(tmp_home) if "admitted" in r]
    assert recs, f"本轮应产生一条完整决策记录（非闸门早退）。stdout={out!r} stderr={err!r}"
    assert recs[-1]["n_topic_words"] == expected


def _seed(home: Path, counts: list, field: str = "n_topic_words",
          topic_on: bool | None = None) -> None:
    """造若干条完整决策记录；`None` 表示**旧记录**（落盘该字段之前的形态）。

    值刻意不限类型：坏值（`True` / `inf` / `nan`）是被测输入之一，见
    `test_one_bad_value_never_kills_the_whole_report`。
    """
    for i, n in enumerate(counts):
        rec = {
            "_schema": 1, "ts": 1.0 + i, "session": "sess-R", "prompt_id": f"p{i}",
            "cwd_h": "abc123", "kw_h": ["h1"], "n_kw": 2, "gate": "", "relaxed": False,
            "admitted": [{"path": "n/a.md", "topical": 7.0, "total": 9.0,
                          "arm": "topical", "dedup": "", "hits": ["内存"]}],
            "near_miss": [], "n_excluded": 3, "ft": {"path": "", "arm": ""},
        }
        if n is not None:
            rec[field] = n
        if topic_on is not None:
            rec["topic_on"] = topic_on
        _metrics.write_record(home, "sess-R", rec)


def test_report_does_not_blame_a_disabled_feature(tmp_path: Path) -> None:
    """功能被显式关掉时，报表不得断言「功能开着却恒空」并给三条不适用的成因。

    `prompt_submit_load` 关掉该功能时整段跳过，`signals.session_topic_words` 恒为空集，
    而接线**无条件**落 `len(...)` ⇒ 每轮都往分母里塞一个 0。`dry_run: true` 同理
    （spawn 门禁含 `not dry_run`，提炼永不发生）。这与本文件既有惯例直接冲突：
    `max_notes` / `min_topical` / `ft_topical` 三个都随记录落自描述配置字段，理由写在
    `analyze_metrics.py` 里——「硬编码后用户一改配置报表就开始说假话」。
    """
    _seed(tmp_path, [0, 0, 0], topic_on=False)
    out = render_report(summarize(load_records(tmp_path)))

    assert "关闭" in out, f"应说明该功能处于关闭状态，实际：{out}"
    assert "一次都没生效" not in out, "不得对被关掉的功能拉响失效告警"


def test_report_still_flags_failure_when_feature_is_on(tmp_path: Path) -> None:
    """阳性对照：功能开着而恒 0 时，那条告警必须照常拉响。

    没有这条，上面那条用例可以被「干脆删掉整段告警」满足。
    """
    _seed(tmp_path, [0, 0, 0], topic_on=True)
    out = render_report(summarize(load_records(tmp_path)))

    assert "一次都没生效" in out


@pytest.mark.parametrize("field,bad", [
    ("n_topic_words", float("inf")),      # json.loads 默认接受 Infinity / 1e999
    ("n_topic_words", float("nan")),
    ("inj_chars", float("inf")),          # 既有字段的同类崩溃，同一处修法一并覆盖
    ("inj_chars", float("nan")),
])
def test_one_bad_value_never_kills_the_whole_report(tmp_path: Path, field, bad) -> None:
    """一条坏值不得让 `--report` 整个失败。

    `_drop_bad_numeric_fields` 的 docstring 把这条写成**反目标原文**（「实测把一条记录的
    `inj_chars` 改成字符串，`--report` 直接 exit=1、stdout 完全为空……而 `--report`
    恰恰是排障入口」），但它的判据 `isinstance(v, (int, float))` **放行非有限浮点**
    —— `isinstance(float('inf'), float)` 为 True，而 `int(inf)` 抛的是 `OverflowError`
    （`int(nan)` 抛 `ValueError`）。于是同一个反目标换个坏值就复发，`inj_chars` 那条是
    修复前就存在的。
    """
    _seed(tmp_path, [1, bad, 2], field=field)
    out = render_report(summarize(load_records(tmp_path)))
    assert out, "报表不得为空"
    assert "session_topic" in out, "合法记录的统计必须仍然产出"


def test_bool_never_counts_as_effective(tmp_path: Path) -> None:
    """`true` 不得被 `int()` 吃成 1。

    `int(True) == 1` 且 `isinstance(True, int)` 为真。一条 `"n_topic_words": true`
    就能把「一次都没生效」这条告警替换成「1/3 轮生效（33.3%）」——而拉响那条告警
    正是这项指标存在的全部理由。坏值必须由 `_NUMERIC_TOP_FIELDS` 单点删键，
    不在这里另写一份判据。
    """
    _seed(tmp_path, [0, 0, True])
    out = render_report(summarize(load_records(tmp_path)))
    assert "一次都没生效" in out, f"bool 被当成了生效，实际：{out}"


def test_report_surfaces_topic_effectiveness(tmp_path: Path) -> None:
    """报表必须报出 session_topic 的生效率，且把旧记录单独摘出去不混进分母。"""
    _seed(tmp_path, [3, 0, 0, None])
    out = render_report(summarize(load_records(tmp_path)))

    assert "session_topic" in out
    assert "1/3" in out, f"应报「3 条有该字段的记录里 1 条生效」，实际：{out}"


def test_report_flags_topic_never_effective(tmp_path: Path) -> None:
    """全部轮次都没读到主题词时，报表必须明说「一次都没生效」。

    这条直接对应 2026-09-09 那次失效的形态：功能开着、提炼在跑、额度在花，
    而读端恒空。报表若只给个 0%，读者仍要自己意识到「0 意味着坏了」；点破它
    才是这栏存在的意义。
    """
    _seed(tmp_path, [0, 0, 0])
    out = render_report(summarize(load_records(tmp_path)))

    assert "一次都没生效" in out, f"恒 0 时应显式点破，实际：{out}"
