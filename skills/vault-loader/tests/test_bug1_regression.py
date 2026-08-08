# -*- coding: utf-8 -*-
"""BUG-1 回归：tag 与 keywords 双命中是最强相关性信号，必须排在仅单命中之前。

真实案例（2026-08-06）：提问含「内存」，笔记 tags 与 keywords 双双含「内存」，
却因去重规则丢掉 prompt_keyword_hit(5)、只拿 prompt_tag_hit×IDF(3.80)，
排名 16/1087 被 max_notes=3 截断；挤掉它的 Top1 仅靠单词「偏离」命中得 7.00。
"""
from scripts._config_loader import DEFAULT_CONFIG
from scripts._frontmatter_reader import Entry
from scripts._scorer import Signals, topical_score, has_keyword_hit

W = DEFAULT_CONFIG["scoring"]


def test_double_hit_outranks_keyword_only():
    double = Entry(path="double.md", tags=("内存",), summary="内存耗尽排查记录说明",
                   keywords=("内存耗尽",), mtime=1_770_000_000)
    single = Entry(path="single.md", tags=(), summary="任务偏离说明文档记录",
                   keywords=("偏离",), mtime=1_770_000_000)
    sig = Signals(prompt_keywords={"内存", "偏离"})
    assert topical_score(double, sig, W) > topical_score(single, sig, W)


def test_double_hit_keeps_full_keyword_weight():
    """双命中时 keywords 权重必须完整计入，不被 tag 命中吞掉。"""
    e = Entry(path="x.md", tags=("内存",), summary="无关摘要占位文本内容",
              keywords=("内存",), mtime=1_770_000_000)
    sig = Signals(prompt_keywords={"内存"})
    # tag(4×IDF=4, 无 tag_df 时 factor=1.0) + keywords(5) = 9；旧行为只有 4
    assert topical_score(e, sig, W) == W["prompt_tag_hit"] + W["prompt_keyword_hit"]


def test_has_keyword_hit_no_longer_dedups():
    e = Entry(path="x.md", tags=("vault-loader",), keywords=("vault-loader", "召回"))
    assert has_keyword_hit(e, {"vault-loader"}) is True
