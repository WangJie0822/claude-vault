"""`--review` 的标注池必须只含人类交互会话的记录。

用户实测反馈：标注界面展示的「你问的是」里混进了不是他敲的内容，例如
「先用 Bash 工具运行 echo MODEL=[$ANTHROPIC_MODEL]，再用 Read 工具读取文件…」。
取证确认该条来自 session `2a5d6de9`，其 transcript 的 `entrypoint` 为 `sdk-cli`
（同屏另外两条来自 `cli` 会话，确是他本人输入）。

为什么闸门不够：`_entrypoint.is_supported_session()` 只挡**新产生**的记录，而标注池
吃的是历史语料——实测 1103 个打分轮次里 35.3% 产自 `claude -p` 一类的程序化调用，
它们的 prompt 是派发指令，拿来问「这篇该不该被召回」毫无意义，而人工标注不可再生。

历史记录没有 entrypoint 字段，按 session 回查 transcript 判定（`--review` 是人工
交互命令，且 `_transcript_index` 本来就要建，成本可接受）。判据常量从
`_entrypoint` 导入，不另立一份——两处判据漂移是本仓库反复吃过亏的形态。
"""

import json

from scripts.analyze_metrics import human_records, session_entrypoint


def _write_transcript(d, session, entrypoint):
    p = d / f"{session}.jsonl"
    p.write_text(json.dumps({"type": "user", "entrypoint": entrypoint,
                             "message": {"content": "hi"}}) + "\n",
                 encoding="utf-8")
    return p


def test_session_entrypoint_reads_transcript(tmp_path):
    idx = {"s1": _write_transcript(tmp_path, "s1", "sdk-cli")}
    assert session_entrypoint("s1", idx) == "sdk-cli"


def test_session_entrypoint_missing_transcript_returns_empty(tmp_path):
    assert session_entrypoint("nope", {}) == ""


def test_human_records_drops_programmatic_sessions(tmp_path):
    """sdk-cli 会话的记录不得进入标注池。

    变异验证：去掉过滤（直接 yield 全部），本用例转红。
    """
    idx = {"human": _write_transcript(tmp_path, "human", "cli"),
           "bot": _write_transcript(tmp_path, "bot", "sdk-cli")}
    recs = [{"session": "human", "prompt_h": "h1"},
            {"session": "bot", "prompt_h": "h2"},
            {"session": "human", "prompt_h": "h3"}]
    got = list(human_records(recs, idx))
    assert [r["prompt_h"] for r in got] == ["h1", "h3"], \
        f"程序化会话的记录没被剔除：{[r['prompt_h'] for r in got]}"


def test_human_records_keeps_unknown_sessions(tmp_path):
    """transcript 不可达时保留 —— 与写端闸门同向：宁可漏禁，不可误删。

    人工标注不可再生，误删一条人类样本的代价高于混进一条程序化样本。
    实测 transcript 可达率 98.9%，影响面很小。

    变异验证：把未知来源也一并剔除，本用例转红。
    """
    idx = {"known": _write_transcript(tmp_path, "known", "cli")}
    recs = [{"session": "known"}, {"session": "gone"}]
    assert len(list(human_records(recs, idx))) == 2


def test_judgement_constant_is_shared_with_writer():
    """判据必须与写端闸门同一来源，不得各写一份。

    变异验证：在 analyze_metrics 里内联一份 ("cli",)，本用例转红。
    """
    import scripts.analyze_metrics as am
    from scripts._entrypoint import INTERACTIVE_ENTRYPOINTS
    assert am.INTERACTIVE_ENTRYPOINTS is INTERACTIVE_ENTRYPOINTS


def test_caches_per_session(tmp_path, monkeypatch):
    """同一 session 只回查一次 —— 标注池上千条记录，逐条读文件不可接受。

    变异验证：去掉缓存字典，本用例转红。
    """
    import scripts.analyze_metrics as am
    _write_transcript(tmp_path, "s", "cli")
    idx = {"s": tmp_path / "s.jsonl"}
    calls = []
    real = am.session_entrypoint
    monkeypatch.setattr(am, "session_entrypoint",
                        lambda s, i: (calls.append(s), real(s, i))[1])
    recs = [{"session": "s"} for _ in range(50)]
    assert len(list(am.human_records(recs, idx))) == 50
    assert len(calls) == 1, f"同一 session 回查了 {len(calls)} 次，缓存没生效"


def test_review_wires_human_filter_into_both_samplers():
    """两个抽样都必须经过 human_records —— 漏一个，那一半标注池照样混入程序化记录。

    源码级断言：`--review` 是交互式入口，端到端验它要 TTY。钉的是**调用形态**而非
    子串存在（注释里写出 human_records 满足不了这条正则），符合本仓库
    「源码级断言必须钉形态」的约定。

    变异验证：把任一处改回 load_records(home)，本用例转红。
    """
    import pathlib as _p
    import re
    src = (_p.Path(__file__).resolve().parent.parent
           / "scripts" / "analyze_metrics.py").read_text(encoding="utf-8")
    # 抽样逻辑已抽进 collect_review_items（命令行版与网页版共用同一份）——
    # 钉那个函数体，而不是某个入口的分支体：入口可能不止一个，钉分支会漏。
    body = src[src.index("def collect_review_items"):]
    body = body[:body.index(chr(10) + "def ", 1)]
    assert len(re.findall(r"sample_near_miss\(\s*human_records\(", body)) == 1,         "near-miss 抽样没有经过 human_records"
    assert len(re.findall(r"sample_admitted\(\s*human_records\(", body)) == 1,         "精度侧抽样没有经过 human_records"
