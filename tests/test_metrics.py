"""B: eval/metrics.py. 손으로 계산한 값과 비교."""
import math

import pytest

from eval import metrics

from eval.metrics import TARGETS, check_targets, evaluate, hit_rate_at_k, mrr, no_evidence_rate, recall_at_k

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


# --- 경계 조건 ---------------------------------------------------------------------
def test_perfect_and_zero():
    perfect = [["a", "b"], ["c", "d"]]
    rel = [{"a"}, {"c"}]
    assert hit_rate_at_k(perfect, rel, 1) == 1.0 and recall_at_k(perfect, rel, 1) == 1.0 and mrr(perfect, rel) == 1.0
    miss = [["x", "y"], ["x", "y"]]
    assert hit_rate_at_k(miss, rel, 5) == 0.0 and recall_at_k(miss, rel, 5) == 0.0 and mrr(miss, rel) == 0.0


def test_k_larger_than_results_and_empty_results():
    assert hit_rate_at_k([["a"]], [{"a"}], 10) == 1.0            # 결과가 k보다 짧아도 된다
    assert hit_rate_at_k([[]], [{"a"}], 5) == 0.0                # 검색 결과가 비어 있으면 실패로 센다
    assert mrr([[]], [{"a"}]) == 0.0


def test_duplicates_count_once():
    assert recall_at_k([["a", "a", "b"]], [{"a", "b"}], 2) == pytest.approx(0.5)   # a가 두 번 나와도 정답 1개
    assert mrr([["x", "a", "a"]], [{"a"}]) == pytest.approx(0.5)                   # 처음 나온 순위만


def test_no_answer_questions_are_skipped():
    retrieved = [["a"], ["zzz"], ["b"]]
    relevant = [{"a"}, set(), {"q"}]                               # 둘째는 근거 없음 질문
    assert hit_rate_at_k(retrieved, relevant, 5) == pytest.approx(0.5)   # 첫째만 적중, 둘째는 분모에서 제외
    result = evaluate(retrieved, relevant, ks=(5,))
    assert result["n_questions"] == 2 and result["n_skipped"] == 1


def test_errors():
    with pytest.raises(ValueError):
        hit_rate_at_k([["a"]], [{"a"}, {"b"}], 5)                  # 질문 수 불일치
    with pytest.raises(ValueError):
        recall_at_k([["a"]], [{"a"}], 0)                           # k < 1
    with pytest.raises(ValueError):
        mrr([["a"]], [set()])                                      # 평가할 질문 없음


def test_no_evidence_rate():
    assert no_evidence_rate([[], ["a"], [], []]) == pytest.approx(0.75)
    with pytest.raises(ValueError):
        no_evidence_rate([])


def test_evaluate_and_targets():
    retrieved = [["a", "b", "c", "d", "e"], ["x", "a", "b", "c", "d"]]
    relevant = [{"a"}, {"a", "z"}]
    r = evaluate(retrieved, relevant)
    assert set(r) >= {"hit_rate@3", "hit_rate@5", "hit_rate@10", "recall@3", "recall@5", "recall@10", "mrr"}
    assert r["hit_rate@5"] == 1.0 and r["recall@5"] == pytest.approx((1.0 + 0.5) / 2)
    assert r["mrr"] == pytest.approx((1 + 1 / 2) / 2)
    assert check_targets(r) == {"hit_rate@5": True, "recall@5": TARGETS["recall@5"] <= 0.75, "mrr": True}
    assert check_targets({"mrr": 0.5}) == {"mrr": False}          # 계산하지 않은 지표는 빠진다


# --- Precision / nDCG / 판정 커버리지 ---------------------------------------------------------------------------
def test_precision_at_k_hand_computed():
    retrieved = [["a", "b", "c", "d"], ["x", "y", "z", "w"]]
    relevant = [{"a", "c", "q"}, {"w"}]
    assert metrics.precision_at_k(retrieved, relevant, 2) == pytest.approx((1 / 2 + 0) / 2)
    assert metrics.precision_at_k(retrieved, relevant, 4) == pytest.approx((2 / 4 + 1 / 4) / 2)


def test_ndcg_perfect_and_graded_order():
    grades = [{"a": 2, "b": 1}]
    assert metrics.ndcg_at_k([["a", "b", "x"]], grades, 3) == pytest.approx(1.0)
    worse = metrics.ndcg_at_k([["b", "a", "x"]], grades, 3)
    expected = (1 / 1 + 2 / math.log2(3)) / (2 / 1 + 1 / math.log2(3))
    assert worse == pytest.approx(expected) and worse < 1.0
    assert metrics.ndcg_at_k([["x", "y"]], grades, 3) == 0.0


def test_ndcg_ignores_repeated_ids_and_questions_without_gold():
    assert metrics.ndcg_at_k([["a", "a", "b"], ["z"]], [{"a": 2}, {}], 3) == pytest.approx(1.0)


def test_judged_rates_skip_empty_results():
    out = metrics.judged_rates([["a", "b", "c", "d"], []], [{"b"}, set()], [{"a", "b"}, set()], 4)
    assert out == {"bad_rate@4": 0.25, "unjudged_rate@4": 0.5}
    with pytest.raises(ValueError):
        metrics.judged_rates([[]], [set()], [set()], 3)


def test_evaluate_graded_adds_judged_keys_only_when_judgments_exist():
    plain = metrics.evaluate_graded([["a", "b"]], [{"a": 2}], [set()], [set()], (2,))
    assert set(plain) == {"precision@2", "ndcg@2"}
    full = metrics.evaluate_graded([["a", "b"]], [{"a": 2}], [{"b"}], [{"a", "b"}], (2,))
    assert full["bad_rate@2"] == 0.5 and full["unjudged_rate@2"] == 0.0
