# -*- coding: utf-8 -*-
from scripts._frontmatter_reader import Entry
from tests.fixtures.vault_shape import extract_shape


def _e(path, tags, summary, keywords, mtime=1_770_000_000):
    return Entry(path=path, tags=tuple(tags), summary=summary,
                 keywords=tuple(keywords), mtime=mtime)


def test_shape_has_required_keys():
    entries = {"a.md": _e("a.md", ["x", "y"], "中文摘要内容", ["kw1"]),
               "b.md": _e("b.md", ["x"], "another summary", [])}
    s = extract_shape(entries)
    for k in ("n_docs", "tag_df_hist", "tags_per_doc", "summary_len",
              "keywords_per_doc", "cjk_ratio", "age_days"):
        assert k in s, k
    assert s["n_docs"] == 2


def test_shape_leaks_no_content():
    """形态参数里不得出现任何原始 tag 名 / 摘要文本 / 路径。"""
    entries = {"secret/path.md": _e("secret/path.md", ["机密标签"], "客户名称摘要", ["密钥"])}
    blob = repr(extract_shape(entries))
    for leaked in ("机密标签", "客户名称摘要", "密钥", "secret", "path.md"):
        assert leaked not in blob, leaked
