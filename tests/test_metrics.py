"""B: eval/metrics.py. 손으로 계산한 값과 비교."""
import pytest

from eval.metrics import hit_rate_at_k, mrr, recall_at_k

RETRIEVED = [["a", "b", "c"], ["x", "y", "z"]]


def test_hit_rate():
    assert hit_rate_at_k(RETRIEVED, [{"b"}, {"q"}], k=3) == pytest.approx(0.5)
    assert hit_rate_at_k(RETRIEVED, [{"c"}, {"x"}], k=1) == pytest.approx(0.5)


def test_recall():
    assert recall_at_k([["a", "b", "c"]], [{"a", "c"}], k=2) == pytest.approx(0.5)
    assert recall_at_k([["a", "b", "c"]], [{"a", "c"}], k=3) == pytest.approx(1.0)


def test_mrr():
    assert mrr(RETRIEVED, [{"b"}, {"q"}]) == pytest.approx(0.25)   # (1/2 + 0) / 2
    assert mrr(RETRIEVED, [{"a"}, {"z"}]) == pytest.approx((1 + 1 / 3) / 2)
