"""Hybrid 매개변수 탐색 (가중치·RRF_K·FIELD_BOOST). 검색 평가(run_eval)와 같은 지표로 조합을 비교한다.

질문마다 Vector·BM25 후보는 매개변수와 무관하므로 한 번만 계산해 두고(캐시), 합치는 단계만 바꿔 가며 다시 돌린다.
따라서 조합이 수십 개여도 임베딩은 질문 수만큼만 계산한다.

  python -m eval.sweep                                  # 기본 격자, K=5
  python -m eval.sweep --bm25 0,0.2,0.5,1 --rrf-k 20,60 --boost 0,0.002

주의: 정답 지정 질문이 수십 개뿐이라 상위 조합 간 차이가 작으면 우연일 수 있다. 질문 세트(검수 후)가 커진 뒤
'확실히 더 좋은' 조합만 config에 반영하고 8.3/9.4에 기록한다.
"""
from __future__ import annotations

import argparse
import itertools
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from eval import run_eval
from src.common import config

RESULTS_DIR = run_eval.RESULTS_DIR
DEFAULT_BM25 = (0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0)
DEFAULT_RRF_K = (10, 30, 60)
DEFAULT_BOOST = (config.FIELD_BOOST,)


def _floats(text: str) -> tuple[float, ...]:
    return tuple(float(x) for x in text.split(",") if x.strip())


@contextmanager
def cached_retrieval():
    """vector_search·bm25_search 결과를 (질문, 후보 수, where)별로 캐시한다. 끝나면 원래대로 돌려놓는다."""
    from src.index import vector_store

    originals = (vector_store.vector_search, vector_store.bm25_search)
    caches: list[dict] = [{}, {}]

    def wrap(fn, cache):
        def inner(query, k, where=None):
            key = (query, k, json.dumps(where, sort_keys=True, default=str))
            if key not in cache:
                cache[key] = fn(query, k, where)
            return cache[key]
        return inner

    vector_store.vector_search = wrap(originals[0], caches[0])
    vector_store.bm25_search = wrap(originals[1], caches[1])
    try:
        yield
    finally:
        vector_store.vector_search, vector_store.bm25_search = originals


@contextmanager
def hybrid_params(vector_weight: float, bm25_weight: float, rrf_k: int, field_boost: float):
    """hybrid 모듈이 읽는 설정을 잠시 바꾼다 (config 값과 rrf 기본 인자)."""
    from src.retrieve import hybrid

    saved = (config.HYBRID_WEIGHTS, config.FIELD_BOOST, hybrid.rrf, hybrid.boost_by_field)
    orig_rrf, orig_boost = hybrid.rrf, hybrid.boost_by_field
    config.HYBRID_WEIGHTS = (vector_weight, bm25_weight)
    config.FIELD_BOOST = field_boost
    hybrid.rrf = lambda rankings, weights=None, rrf_k_=rrf_k, **kw: orig_rrf(rankings, weights, rrf_k=rrf_k_)
    hybrid.boost_by_field = lambda ranked, wanted, boost=field_boost: orig_boost(ranked, wanted, boost)
    try:
        yield
    finally:
        config.HYBRID_WEIGHTS, config.FIELD_BOOST, hybrid.rrf, hybrid.boost_by_field = saved


def run_sweep(questions: list[dict], k: int, bm25_weights, rrf_ks, boosts, today: str | None = None,
              experiment: str = "hybrid_filter") -> list[dict]:
    rows = []
    with cached_retrieval():
        for bm25_w, rrf_k, boost in itertools.product(bm25_weights, rrf_ks, boosts):
            with hybrid_params(1.0, bm25_w, rrf_k, boost):
                result = run_eval.run_experiment(experiment, questions, run_eval.make_search_fn(experiment, today), k, today)
            s = result["metrics"]
            rows.append({"bm25_weight": bm25_w, "rrf_k": rrf_k, "field_boost": boost,
                         "hit_rate": s[f"hit_rate@{k}"], "recall": s[f"recall@{k}"], "mrr": s["mrr"]})
    return sorted(rows, key=lambda r: (-r["mrr"], -r["hit_rate"], -r["recall"]))


def format_table(rows: list[dict], k: int, top: int = 15) -> str:
    lines = [f"| BM25 가중치 | RRF_K | FIELD_BOOST | Hit Rate@{k} | Recall@{k} | MRR |", "|---|---|---|---|---|---|"]
    for r in rows[:top]:
        lines.append(f"| {r['bm25_weight']:g} | {r['rrf_k']} | {r['field_boost']:g} | {r['hit_rate']:.3f} | {r['recall']:.3f} | {r['mrr']:.3f} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Hybrid 가중치·RRF_K·FIELD_BOOST 탐색")
    ap.add_argument("--bm25", type=_floats, default=DEFAULT_BM25, help="BM25 가중치 목록 (Vector는 1.0 고정, 쉼표 구분)")
    ap.add_argument("--rrf-k", type=lambda t: tuple(int(x) for x in t.split(",")), default=DEFAULT_RRF_K)
    ap.add_argument("--boost", type=_floats, default=DEFAULT_BOOST, help="FIELD_BOOST 목록")
    ap.add_argument("--k", type=int, default=config.DEFAULT_TOP_K)
    ap.add_argument("--top", type=int, default=15, help="표에 보여 줄 상위 조합 수")
    ap.add_argument("--questions", default=str(run_eval.QUESTIONS_PATH))
    ap.add_argument("--today")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args(argv)

    from src.index import vector_store

    if vector_store.count() == 0:
        raise SystemExit("색인이 비어 있습니다. 먼저 python -m src.ingest.handoff --apply")
    questions = run_eval.load_questions(args.questions)
    rows = run_sweep(questions, args.k, args.bm25, args.rrf_k, args.boost, args.today)
    ref = next((r for r in rows if r["bm25_weight"] == 1.0 and r["rrf_k"] == config.RRF_K and r["field_boost"] == config.FIELD_BOOST), None)
    print(f"조합 {len(rows)}개 (질문 {len(run_eval.split_questions(questions)[0])}개 평가, K={args.k}). MRR 순:\n")
    print(format_table(rows, args.k, args.top))
    if ref:
        print(f"\n현재 설정(BM25 1.0 / RRF_K {config.RRF_K} / FIELD_BOOST {config.FIELD_BOOST:g}): "
              f"Hit {ref['hit_rate']:.3f}  Recall {ref['recall']:.3f}  MRR {ref['mrr']:.3f}")
    if not args.no_save:
        path = Path(RESULTS_DIR) / f"sweep_k{args.k}_{datetime.now():%Y%m%d-%H%M%S}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"저장: {path}")


if __name__ == "__main__":
    main()
