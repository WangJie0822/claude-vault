# -*- coding: utf-8 -*-
from tests._metrics import ndcg_at_k, mrr, recall_at_k


def test_ndcg_perfect_ranking_is_one():
    rel = {"a": 2, "b": 1}
    assert ndcg_at_k(["a", "b", "c"], rel, 10) == 1.0


def test_ndcg_reversed_ranking_is_lower():
    rel = {"a": 2, "b": 1}
    good = ndcg_at_k(["a", "b"], rel, 10)
    bad = ndcg_at_k(["b", "a"], rel, 10)
    assert bad < good


def test_ndcg_irrelevant_only_is_zero():
    assert ndcg_at_k(["x", "y"], {"a": 2}, 10) == 0.0


def test_mrr_first_position():
    assert mrr(["a", "b"], {"a": 1}) == 1.0
    assert mrr(["b", "a"], {"a": 1}) == 0.5
    assert mrr(["x"], {"a": 1}) == 0.0


def test_recall_at_k():
    assert recall_at_k(["a", "b", "x"], {"a": 1, "b": 1}, 3) == 1.0
    assert recall_at_k(["a", "x", "y"], {"a": 1, "b": 1}, 3) == 0.5
