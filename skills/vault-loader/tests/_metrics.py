# -*- coding: utf-8 -*-
"""排序质量指标（纯函数，无 IO）。ranked 为按分数降序的 path 列表。"""
from __future__ import annotations

import math


def dcg(gains: list[float]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked: list[str], relevant: dict[str, int], k: int) -> float:
    """归一化折损累积增益。无相关项时返回 0.0。"""
    if not relevant:
        return 0.0
    gains = [float(relevant.get(p, 0)) for p in ranked[:k]]
    ideal = sorted((float(v) for v in relevant.values()), reverse=True)[:k]
    idcg = dcg(ideal)
    return dcg(gains) / idcg if idcg > 0 else 0.0


def mrr(ranked: list[str], relevant: dict[str, int]) -> float:
    """首个相关项的倒数排名。"""
    for i, p in enumerate(ranked):
        if relevant.get(p, 0) > 0:
            return 1.0 / (i + 1)
    return 0.0


def recall_at_k(ranked: list[str], relevant: dict[str, int], k: int) -> float:
    if not relevant:
        return 0.0
    hit = sum(1 for p in ranked[:k] if relevant.get(p, 0) > 0)
    return hit / len(relevant)
