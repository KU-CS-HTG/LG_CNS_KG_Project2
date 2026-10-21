"""B: eval/sweep.py (Hybrid 매개변수 탐색). 샘플 색인(가짜 임베딩)으로 시험한다."""
from pathlib import Path

import pytest

from eval import run_eval, sweep
from src.common import config

SAMPLE_QUESTIONS = Path(__file__).parent / "fixtures" / "sample_eval_questions.jsonl"


@pytest.fixture
def questions():
    return run_eval.load_questions(SAMPLE_QUESTIONS)


def test_run_sweep_grid_sorted_and_params_restored(indexed_sample, questions):
    from src.retrieve import hybrid

    before = (config.HYBRID_WEIGHTS, config.FIELD_BOOST, hybrid.rrf, hybrid.boost_by_field)
    rows = sweep.run_sweep(questions, 5, (0.0, 0.5, 1.0), (10, 60), (0.0, 0.002), today="2026-10-19")
    assert len(rows) == 3 * 2 * 2
    mrrs = [r["mrr"] for r in rows]
    assert mrrs == sorted(mrrs, reverse=True)
    assert all(0 <= r["hit_rate"] <= 1 and 0 <= r["recall"] <= 1 for r in rows)
    assert (config.HYBRID_WEIGHTS, config.FIELD_BOOST, hybrid.rrf, hybrid.boost_by_field) == before   # 원상 복구


def test_params_change_results_and_match_run_eval_default(indexed_sample, questions):
    """기본 설정과 같은 조합은 run_eval 결과와 정확히 같아야 한다 (탐색 도구가 평가와 다른 계산을 하지 않음)."""
    base = run_eval.run_experiment("hybrid_filter", questions, run_eval.make_search_fn("hybrid_filter", "2026-10-19"), 5, "2026-10-19")["metrics"]
    rows = sweep.run_sweep(questions, 5, (config.HYBRID_WEIGHTS[1],), (config.RRF_K,), (config.FIELD_BOOST,), today="2026-10-19")
    assert rows[0]["mrr"] == pytest.approx(base["mrr"]) and rows[0]["hit_rate"] == pytest.approx(base["hit_rate@5"])


def test_retrieval_is_cached_across_combinations(indexed_sample, questions, monkeypatch):
    from src.index import vector_store

    calls = {"vec": 0}
    real = vector_store.vector_search

    def counting(query, k, where=None):
        calls["vec"] += 1
        return real(query, k, where)

    monkeypatch.setattr(vector_store, "vector_search", counting)
    sweep.run_sweep(questions, 5, (0.2, 1.0), (10, 60), (0.0,), today="2026-10-19")
    n_scored = len(run_eval.split_questions(questions)[0]) + len(run_eval.split_questions(questions)[1])
    assert calls["vec"] <= n_scored * 2           # 4개 조합이어도 질문마다 한두 번(필터 완화 재시도 포함)만 계산
    assert vector_store.vector_search is counting  # 끝나면 원래 함수 복구


def test_format_table():
    rows = [{"bm25_weight": 0.3, "rrf_k": 60, "field_boost": 0.002, "hit_rate": 0.6, "recall": 0.3, "mrr": 0.45}]
    out = sweep.format_table(rows, 5)
    assert "| 0.3 | 60 | 0.002 | 0.600 | 0.300 | 0.450 |" in out and "Hit Rate@5" in out
