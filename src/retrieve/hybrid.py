"""⑤ 검색 - Hybrid(Vector + BM25), Top-K, 서비스 단위 중복 제거. B -> C 인터페이스.

흐름 (mode="hybrid_filter", 서비스 기본값)
  1. 정형 필터를 벡터 저장소 조건(where)으로 바꾼다 (소프트: 값이 비어 있으면 통과).
     거르는 조건은 **신청기한·나이·지역**뿐이다. 관심 분야(field)는 자격이 아니라 관심사라 거르지 않는다.
  2. Vector 검색과 BM25 검색을 각각 후보 HYBRID_CANDIDATES개씩 가져온다
  3. RRF(Reciprocal Rank Fusion)로 두 순위를 합친다: 점수 = Σ 가중치 / (RRF_K + 순위)
     - 두 검색의 점수 단위(코사인 vs BM25)가 달라 점수를 직접 더하지 않고 순위만 쓴다
  3''. 질문에 지역이 없으면(hybrid_filter) 지자체 제도 점수에서 LOCAL_PENALTY를 뺀다 (전국 제도 우선, 0이면 끔)
  3'. 관심 분야가 제도의 분야와 맞으면(`filters.field_matches`) 점수에 FIELD_BOOST를 더한다 (가산점일 뿐 제도를 빼지 않는다.
     분야 이름이 데이터와 달라 하나도 안 맞아도 결과는 그대로 나온다. FIELD_BOOST=0이면 끔)
  4. 같은 서비스의 조각은 가장 높은 것 하나만 남기고(서비스당 1개) 상위 K개를 돌려준다
  5. 결과가 없으면 filters.relax()로 조건을 한 단계씩 풀어 다시 검색한다 (지역 -> 연령. 분야는 필터가 아니라 풀지 않음)
  0. (hybrid_filter) 가장 가까운 조각의 유사도가 MIN_SIMILARITY보다 낮으면 결과 없음 (아주 엉뚱한 질문, below_floor=True)

mode는 평가 실험용이다 (필터 없이 Vector 단독 / BM25 단독 / Hybrid / Hybrid+정형 필터 비교).
Reranking·MMR은 문제가 관찰되면 도입한다 (8.2 개선 방법 표에 기록).

확인: python -m src.retrieve.hybrid "월세가 부담돼요" --age 27 --region 서울   (먼저 색인이 있어야 한다)
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Literal

from src.common import config
from src.common.schema import Chunk, Filters
from src.index import vector_store
from src.retrieve import filters as filter_rules

# 가장 가까운 조각의 코사인 유사도가 이보다 낮으면 근거 없음([]) (hybrid_filter, 0이면 끔). bge-m3 기준 2026-10-28:
# 정답 있는 평가 질문 최저 0.616, 실제로 제도가 있는 드문 주제('골프 레슨비' 0.607), 근거 없음 '해외 주식 손실 보전' 0.552·
# '비트코인 손실 보전' 0.532. 근거 없음 질문 대부분('청년희망적금 가입', '내년에 생길 지원금')은 0.65~0.71이라 이 값으로는
# 못 가르고 답변 단계 판정이 가른다. 이 하한은 아주 엉뚱한 질문을 일찍 끊어 판정·생성 LLM 호출을 아끼는 용도다.
# 모델마다 유사도 크기가 달라 모델별로 정하고, 표에 없는 모델·가짜 임베딩(테스트)에서는 끈다 (_min_similarity)
MIN_SIMILARITY = {"BAAI/bge-m3": 0.58}


def _min_similarity() -> float:
    """지금 질문 임베딩에 실제로 쓴 모델의 유사도 하한. 모델을 불러오지 않았으면(가짜 임베딩) 0 (끔)."""
    from src.index import embedder
    return MIN_SIMILARITY.get(embedder._model_name or "", 0.0) if embedder._model is not None else 0.0

Mode = Literal["vector", "bm25", "hybrid", "hybrid_filter"]
MODES = ("vector", "bm25", "hybrid", "hybrid_filter")


@dataclass
class SearchResult:
    """search()의 상세 결과 (평가·디버깅용). chunks는 서비스당 1개, 관련도 순."""
    chunks: list[Chunk]
    scores: list[float]                       # chunks와 같은 순서 (vector: 코사인, bm25: BM25, hybrid: RRF)
    filters_used: Filters = field(default_factory=dict)   # 실제로 적용된 필터 (완화됐으면 원래보다 적다)
    relaxed: bool = False                     # 결과가 없어 필터를 푼 적이 있는가
    below_floor: bool = False                 # 가장 가까운 조각도 유사도 하한(MIN_SIMILARITY) 미달이라 비었는가 (근거 없음 질문)


def dedupe(chunks: list[Chunk]) -> list[Chunk]:
    """같은 service_id의 조각 중 먼저 나온(관련도 높은) 것 하나만 남긴다. 순서 유지."""
    seen: set[str] = set()
    out: list[Chunk] = []
    for c in chunks:
        if c.service_id not in seen:
            seen.add(c.service_id)
            out.append(c)
    return out


def rrf(rankings: list[list[Chunk]], weights: tuple[float, ...] | None = None,
        rrf_k: int = config.RRF_K) -> list[tuple[Chunk, float]]:
    """순위 목록 여러 개를 RRF로 합친다. 점수 높은 순으로 (조각, 점수)를 돌려준다."""
    weights = weights or (1.0,) * len(rankings)
    scores: dict[str, float] = {}
    chunks: dict[str, Chunk] = {}
    for ranking, weight in zip(rankings, weights):
        for rank, chunk in enumerate(ranking, start=1):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + weight / (rrf_k + rank)
            chunks.setdefault(chunk.chunk_id, chunk)
    order = sorted(scores, key=lambda cid: -scores[cid])   # 동점이면 먼저 나온 순서 (sorted는 안정 정렬)
    return [(chunks[cid], scores[cid]) for cid in order]


def boost_by_field(ranked: list[tuple[Chunk, float]], wanted_field: str | None,
                   boost: float = config.FIELD_BOOST) -> list[tuple[Chunk, float]]:
    """관심 분야와 맞는 조각의 점수에 boost를 더하고 다시 정렬한다 (안정 정렬: 동점이면 원래 순서). 거르지는 않는다."""
    if not wanted_field or boost <= 0:
        return ranked
    boosted = [(c, s + boost if filter_rules.field_matches(c.metadata.get("field"), wanted_field) else s) for c, s in ranked]
    return sorted(boosted, key=lambda cs: -cs[1])


def penalize_local(ranked: list[tuple[Chunk, float]], penalty: float) -> list[tuple[Chunk, float]]:
    """지자체(지역) 제도의 점수에서 penalty를 빼고 다시 정렬한다 (안정 정렬). 거르지는 않는다.

    질문에 지역이 없으면 어느 지역 제도인지 알 수 없으므로 전국 단위 제도를 먼저 보여 주려는 실험이다."""
    if penalty <= 0:
        return ranked
    adjusted = [(c, s - penalty if filter_rules.is_local(c) else s) for c, s in ranked]
    return sorted(adjusted, key=lambda cs: -cs[1])


def _top_services(ranked: list[tuple[Chunk, float]], k: int) -> tuple[list[Chunk], list[float]]:
    seen: set[str] = set()
    chunks: list[Chunk] = []
    scores: list[float] = []
    for chunk, score in ranked:
        if chunk.service_id in seen:
            continue
        seen.add(chunk.service_id)
        chunks.append(chunk)
        scores.append(score)
        if len(chunks) == k:
            break
    return chunks, scores


def _run(query: str, k: int, mode: Mode, where: dict | None, wanted_field: str | None = None,
         local_penalty: float = 0.0, min_similarity: bool = False) -> tuple[list[Chunk], list[float], bool]:
    """(조각, 점수, 유사도 하한 미달). min_similarity면 가장 가까운 조각의 코사인 유사도가 모델별 하한보다 낮을 때 빈 결과."""
    n = max(config.HYBRID_CANDIDATES, k * 5)
    if mode == "vector":
        return (*_top_services(vector_store.vector_search(query, n, where), k), False)
    if mode == "bm25":
        return (*_top_services(vector_store.bm25_search(query, n, where), k), False)
    vec_scored = vector_store.vector_search(query, n, where)
    floor = _min_similarity() if min_similarity else 0.0     # 검색이 질문 임베딩(모델 불러오기)을 마친 뒤에 본다
    if floor > 0 and vec_scored and max(s for _, s in vec_scored) < floor:
        return [], [], True
    vec = [c for c, _ in vec_scored]
    kw = [c for c, _ in vector_store.bm25_search(query, n, where)]
    fused = boost_by_field(rrf([vec, kw], config.HYBRID_WEIGHTS), wanted_field)
    return (*_top_services(penalize_local(fused, local_penalty), k), False)


def search_with_info(query: str, filters: Filters, k: int = config.DEFAULT_TOP_K, mode: Mode = "hybrid_filter") -> SearchResult:
    """search()와 같지만 점수와 실제 적용된 필터까지 돌려준다."""
    if mode not in MODES:
        raise ValueError(f"mode는 {MODES} 중 하나여야 합니다: {mode!r}")
    if mode != "hybrid_filter":                       # 필터 없이 비교하는 실험 모드
        chunks, scores, _ = _run(query, k, mode, None)
        return SearchResult(chunks, scores, {}, False)

    current: Filters = dict(filters)                   # type: ignore[assignment]
    relaxed = False
    while True:
        penalty = 0.0 if current.get("region") else config.LOCAL_PENALTY      # 지역을 모를 때만 지역 제도를 뒤로
        chunks, scores, below = _run(query, k, mode, vector_store.build_where(current), current.get("field"), penalty,
                                     min_similarity=True)
        if chunks:
            return SearchResult(chunks, scores, current, relaxed)
        softer = filter_rules.relax(current)
        if softer == current:                          # 더 풀 조건이 없다: 근거 없음
            return SearchResult([], [], current, relaxed, below_floor=below)
        current, relaxed = softer, True


def search(
    query: str,
    filters: Filters,
    k: int = config.DEFAULT_TOP_K,
    mode: Mode = "hybrid_filter",
) -> list[Chunk]:
    """질문 하나에 대한 Top-K 조각 (서비스당 1개, 관련도 순). 결과가 없으면 필터를 풀어 다시 찾고,
    그래도 없으면 빈 리스트(C가 '근거 없음'으로 답한다)."""
    return search_with_info(query, filters, k, mode).chunks


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="검색 확인 (먼저 python -m src.index.vector_store --sample 로 색인)")
    ap.add_argument("query")
    ap.add_argument("--age", type=int)
    ap.add_argument("--region")
    ap.add_argument("--field", help="관심 분야. 필터가 아니라 맞는 제도에 가산점")
    ap.add_argument("--today", help="기준일 (예: 2026-10-19). 기본은 오늘")
    ap.add_argument("--k", type=int, default=config.DEFAULT_TOP_K)
    ap.add_argument("--mode", choices=MODES, default="hybrid_filter")
    args = ap.parse_args(argv)

    given = {key: val for key, val in (("age", args.age), ("region", args.region), ("field", args.field), ("today", args.today)) if val is not None}
    result = search_with_info(args.query, given, args.k, args.mode)  # type: ignore[arg-type]
    print(f"[{args.mode}] 필터: {given or '없음'}" + (f"  → 완화됨: {result.filters_used}" if result.relaxed else ""))
    if not result.chunks:
        print("  (결과 없음 → C는 '근거 없음'으로 답해야 함)")
    for rank, (chunk, score) in enumerate(zip(result.chunks, result.scores), start=1):
        print(f" {rank}. {score:7.4f}  {chunk.service_id}  {chunk.service_name}  [{chunk.section}]  {chunk.metadata.get('region', '-')}")


if __name__ == "__main__":
    main()
