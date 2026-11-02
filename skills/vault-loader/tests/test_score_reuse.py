"""同轮话题分复用：结果等价与减少执行次数分别守卫。"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import io
import json
from types import SimpleNamespace

import pytest

from scripts import _decision, _scorer
from scripts._config_loader import DEFAULT_CONFIG
from scripts._decision import StateView
from scripts._frontmatter_reader import Entry
from scripts._scorer import Signals
from tests.integration.test_perf import large_vault  # noqa: F401，复用原500篇夹具


@pytest.mark.parametrize("matched", [False, True])
@pytest.mark.parametrize("use_keywords", [False, True])
def test_precomputed_topic_preserves_score_without_recomputing(monkeypatch, matched, use_keywords):
    entry = Entry(path="a.md", tags=("hook",) if matched else (),
                  summary="注入设计" if matched else "无关内容", keywords=("召回",), mtime=1000)
    signals = Signals(prompt_keywords={"hook", "注入"}, session_topic_words={"设计"})
    weights = DEFAULT_CONFIG["scoring"]
    topical = _scorer.topical_score(entry, signals, weights, use_keywords)
    expected = _scorer.score(entry, signals, weights, use_keywords)
    assert (topical > 0) is matched
    calls = []
    original = _scorer._prompt_topical_hits

    def observe(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(_scorer, "_prompt_topical_hits", observe)
    assert _scorer.score(entry, signals, weights, use_keywords, precomputed_topic=topical) == expected
    assert calls == [], "有效0分也须复用，不能再次计算"
    assert _scorer.score(entry, signals, weights, use_keywords) == expected
    assert calls == [1], "未提供预计算值时仍保留原API计算行为"


def _compare_decision(monkeypatch, entries, signals, config, state, auto_fulltext=True):
    """基准只去掉可选参数，强制使用独立的原有score默认计算路径。"""
    original_score = _scorer.score
    original_hits = _scorer._prompt_topical_hits
    counts = Counter()

    def observed(entry, *args, **kwargs):
        counts[entry.path] += 1
        return original_hits(entry, *args, **kwargs)

    with monkeypatch.context() as local:
        # 只固定被测模块的时钟，不改全局time。
        local.setattr(_scorer, "time", SimpleNamespace(time=lambda: 1900000000.0))
        local.setattr(_scorer, "_prompt_topical_hits", observed)
        actual = _decision.decide_injection(entries, signals, config["scoring"], config,
                                            state, auto_fulltext=auto_fulltext)
        assert counts == Counter({path: 1 for path in entries}), "每次决策每篇话题分只执行一次"

        def reference(*args, **kwargs):
            kwargs.pop("precomputed_topic", None)
            return original_score(*args, **kwargs)

        local.setattr(_decision, "score", reference)
        expected = _decision.decide_injection(entries, signals, config["scoring"], config,
                                              state, auto_fulltext=auto_fulltext)
    assert actual == expected, "完整Decision（含总分、排序、命中链与全文）必须保持"
    return actual


@pytest.mark.parametrize("use_keywords", [False, True])
@pytest.mark.parametrize("use_tag_idf", [False, True])
@pytest.mark.parametrize("auto_fulltext", [False, True])
def test_decision_reuses_only_current_entry_score(monkeypatch, use_keywords, use_tag_idf, auto_fulltext):
    config = deepcopy(DEFAULT_CONFIG)
    config["relevance"].update(use_keywords=use_keywords, use_tag_idf=use_tag_idf,
                                min_topical_score=0, tag_idf_floor=0.3)
    entries = {
        "a.md": Entry(path="a.md", tags=("hook", "skill"), summary="hook 注入 设计", keywords=("召回",), mtime=1899999900),
        "b.md": Entry(path="b.md", tags=("skill",), summary="skill 配置", keywords=(), mtime=1890000000),
        "zero.md": Entry(path="zero.md", tags=(), summary="unrelated webhook", keywords=(), mtime=1000),
        "excluded.md": Entry(path="excluded.md", tags=("hook",), summary="设计", keywords=(), mtime=1000),
        "full.md": Entry(path="full.md", tags=("hook",), summary="hook skill", keywords=(), mtime=1000),
    }
    signals = Signals(prompt_keywords={"hook", "skill", "注入"}, session_topic_words={"设计"},
                      project_dir_paths={"b.md"}, commit_keywords={"hook"}, worklog_keywords={"skill"})
    decision = _compare_decision(monkeypatch, entries, signals, config,
                                StateView(candidate_injected={"a.md"}, user_excluded={"excluded.md"},
                                          fulltext_injected={"full.md"}), auto_fulltext)
    assert any(d.path == "zero.md" and d.topical == 0 for d in decision.admitted)


def test_empty_decision_keeps_default_behavior(monkeypatch):
    _compare_decision(monkeypatch, {}, Signals(prompt_keywords={"hook", "skill"}),
                      DEFAULT_CONFIG, StateView())


def test_actual_large_vault_hook_reuses_scores(monkeypatch, tmp_home, large_vault, capsys):
    from scripts import prompt_submit_load as hook

    config_dir = tmp_home / ".context-vault"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps({"vault_path": str(large_vault),
        "relevance": {"fine_rank": False, "session_topic": False}}), encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_home / "empty-bin"))
    seen = []

    def observed(entries, signals, weights, config, state, **kwargs):
        assert len(entries) == 500
        result = _compare_decision(monkeypatch, entries, signals, config, state, **kwargs)
        seen.append(result)
        return result

    monkeypatch.setattr(hook, "decide_injection", observed)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(tmp_home),
        "prompt": "召回 扩展词 相关性打分 回归测试 语义检索 关键词匹配 怎么优化实现",
        "session_id": "sess-score-reuse", "hook_event_name": "UserPromptSubmit"})))
    hook.main()
    output = json.loads(capsys.readouterr().out)
    assert len(seen) == 1 and seen[0].admitted
    assert output["hookSpecificOutput"]["additionalContext"]
