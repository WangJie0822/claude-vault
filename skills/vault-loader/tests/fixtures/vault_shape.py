# -*- coding: utf-8 -*-
"""从真实 Vault 提取**统计形态**，供 gold 语料参数化生成。

只输出统计量（分布、分位数、比例），绝不输出 tag 名 / 摘要文本 / 路径——
gold_corpus 的不变量是「语料全合成、不得复制任何真实 prompt 片段」，且该文件
随插件分发给每个安装者。
"""
from __future__ import annotations

import time


def _quantiles(vals: list[float], qs=(0.1, 0.5, 0.9)) -> list[float]:
    if not vals:
        return [0.0 for _ in qs]
    s = sorted(vals)
    return [float(s[min(int(q * len(s)), len(s) - 1)]) for q in qs]


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for c in text if 0x4E00 <= ord(c) <= 0x9FFF)
    return cjk / len(text)


def extract_shape(entries: dict) -> dict:
    """entries: {path: Entry}。返回纯统计量字典。"""
    n = len(entries)
    df: dict[str, int] = {}
    tags_per, kw_per, sum_len, cjk, ages = [], [], [], [], []
    now = time.time()
    for e in entries.values():
        tags = tuple(e.tags or ())
        for t in set(t.lower() for t in tags):
            df[t] = df.get(t, 0) + 1
        tags_per.append(len(tags))
        kw_per.append(len(e.keywords or ()))
        sum_len.append(len(e.summary or ""))
        cjk.append(_cjk_ratio(e.summary or ""))
        if e.mtime:
            ages.append((now - e.mtime) / 86400)

    # tag 文档频次分桶：singleton / 稀有(2-5) / 常见(6-50) / 泛(>50)
    hist = {"singleton": 0, "rare": 0, "common": 0, "broad": 0}
    for c in df.values():
        if c == 1:
            hist["singleton"] += 1
        elif c <= 5:
            hist["rare"] += 1
        elif c <= 50:
            hist["common"] += 1
        else:
            hist["broad"] += 1

    return {
        "n_docs": n,
        "n_distinct_tags": len(df),
        "tag_df_hist": hist,
        "tags_per_doc": _quantiles([float(x) for x in tags_per]),
        "keywords_per_doc": _quantiles([float(x) for x in kw_per]),
        "summary_len": _quantiles([float(x) for x in sum_len]),
        "cjk_ratio": round(sum(cjk) / n, 4) if n else 0.0,
        "age_days": _quantiles(ages),
    }
