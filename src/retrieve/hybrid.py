"""⑤ 검색 - Hybrid(Vector + BM25), Top-K, 서비스 단위 중복 제거. B -> C 인터페이스.

흐름 (mode="hybrid_filter", 서비스 기본값)
  1. 정형 필터를 벡터 저장소 조건(where)으로 바꾼다 (소프트: 값이 비어 있으면 통과, 기한 지난 제도 제외)
  2. Vector 검색과 BM25 검색을 각각 후보 HYBRID_CANDIDATES개씩 가져온다
  3. RRF(Reciprocal Rank Fusion)로 두 순위를 합친다: 점수 = Σ 가중치 / (RRF_K + 순위)
     - 두 검색의 점수 단위(코사인 vs BM25)가 달라 점수를 직접 더하지 않고 순위만 쓴다
  4. 같은 서비스의 조각은 가장 높은 것 하나만 남기고(서비스당 1개) 상위 K개를 돌려준다
  5. 결과가 없으면 filters.relax()로 조건을 한 단계씩 풀어 다시 검색한다 (분야 -> 지역 -> 연령)

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

Mode = Literal["vector", "bm25", "hybrid", "hybrid_filter"]
MODES = ("vector", "bm25", "hybrid", "hybrid_filter")


@dataclass
class SearchResult:
    """search()의 상세 결과 (평가·디버깅용). chunks는 서비스당 1개, 관련도 순."""
    chunks: list[Chunk]
    scores: list[float]                       # chunks와 같은 순서 (vector: 코사인, bm25: BM25, hybrid: RRF)
    filters_used: Filters = field(default_factory=dict)   # 실제로 적용된 필터 (완화됐으면 원래보다 적다)
    relaxed: bool = False                     # 결과가 없어 필터를 푼 적이 있는가


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


def _run(query: str, k: int, mode: Mode, where: dict | None) -> tuple[list[Chunk], list[float]]:
    n = max(config.HYBRID_CANDIDATES, k * 5)
    if mode == "vector":
        return _top_services(vector_store.vector_search(query, n, where), k)
    if mode == "bm25":
        return _top_services(vector_store.bm25_search(query, n, where), k)
    vec = [c for c, _ in vector_store.vector_search(query, n, where)]
    kw = [c for c, _ in vector_store.bm25_search(query, n, where)]
    return _top_services(rrf([vec, kw], config.HYBRID_WEIGHTS), k)


def search_with_info(query: str, filters: Filters, k: int = config.DEFAULT_TOP_K, mode: Mode = "hybrid_filter") -> SearchResult:
    """search()와 같지만 점수와 실제 적용된 필터까지 돌려준다."""
    if mode not in MODES:
        raise ValueError(f"mode는 {MODES} 중 하나여야 합니다: {mode!r}")
    if mode != "hybrid_filter":                       # 필터 없이 비교하는 실험 모드
        chunks, scores = _run(query, k, mode, None)
        return SearchResult(chunks, scores, {}, False)

    current: Filters = dict(filters)                   # type: ignore[assignment]
    relaxed = False
    while True:
        chunks, scores = _run(query, k, mode, vector_store.build_where(current))
        if chunks:
            return SearchResult(chunks, scores, current, relaxed)
        softer = filter_rules.relax(current)
        if softer == current:                          # 더 풀 조건이 없다: 근거 없음
            return SearchResult([], [], current, relaxed)
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
    ap.add_argument("--field")
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
