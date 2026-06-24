# skills/vault-loader/tests/test_injection_guard.py
from scripts import prompt_submit_load as p


def test_fulltext_injection_has_notice():
    # 构造全文注入文本，断言含隔离声明关键词
    text = p.build_fulltext_injection("SampleNote", "正文内容")  # 见 Step 3 抽出的函数
    assert "知识库历史内容" in text or "non-instruction" in text.lower()
