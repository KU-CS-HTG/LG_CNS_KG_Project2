"""판정 기준 문서 색인·검색 (X1, CLAUDE.md B -> C 계약 search_guides).

보조금24 색인(data/vectorstore, Chroma)과 섞이지 않게 따로 둔다: data/guides/_index/ (git 제외).
조각 2천 개 안팎이라 Chroma 대신 벡터 배열(.npy) + BM25로 충분하다. 문서가 바뀔 때만 다시 만든다.
- 임베딩: 보조금24와 같은 embedder(bge-m3). 조각 앞에 "문서명 > 장 > 절" 제목을 붙여 임베딩한다.
- BM25: vector_store와 같은 글자 2-gram 토큰화.
- 순위: Vector와 BM25의 RRF. 판정 문서는 조항 낱말('부양의무자', '근로소득공제')이 중요해 가중치를 같게 둔다
  (보조금24 HYBRID_WEIGHTS는 서비스 검색용으로 튜닝한 값이라 쓰지 않는다).
색인 만들기: python -m src.ingest.guides 다음 python -m src.index.guide_store
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import threading
from pathlib import Path

import numpy as np

from src.common import config
from src.common.regions import normalize_region
from src.common.schema import GuideChunk
from src.ingest import guides

log = logging.getLogger(__name__)

INDEX_DIR = guides.GUIDES_DIR / "_index"
CANDIDATES = 50                 # Vector·BM25 각각에서 가져올 후보 수
WEIGHTS = (1.0, 1.0)            # (Vector, BM25)

_lock = threading.Lock()
_state: dict | None = None      # {"dir", "chunks", "vectors", "bm25"} 처음 검색할 때 읽는다


def embed_text(c: GuideChunk) -> str:
    return f"{c.heading}\n{c.text}"


def _tokenize(text: str) -> list[str]:
    from src.index.vector_store import _tokenize as tokenize   # 보조금24 BM25와 같은 토큰화
    return tokenize(text)


def build(chunks: list[GuideChunk] | None = None, index_dir: Path = INDEX_DIR,
          guides_dir: Path = guides.GUIDES_DIR) -> int:
    """조각을 임베딩해 index_dir에 저장한다. chunks가 없으면 <guides_dir>/guide_chunks.jsonl을 읽는다."""
    global _state
    from src.index import embedder
    if chunks is None:
        path = Path(guides_dir) / guides.CHUNKS_NAME
        if not path.exists():
            raise FileNotFoundError(f"{path} 없음. 먼저 python -m src.ingest.guides 를 실행하세요.")
        chunks = guides.read_chunks(path)
    index_dir = Path(index_dir)
    texts = [embed_text(c) for c in chunks]
    old = _previous_vectors(index_dir)
    todo = sorted({t for t in texts if t not in old})
    if todo:
        new = np.asarray(embedder.embed_texts(todo, kind="passage"), dtype=np.float32)
        new /= np.maximum(np.linalg.norm(new, axis=1, keepdims=True), 1e-12)
        old.update(zip(todo, new))
    vectors = np.stack([old[t] for t in texts]) if texts else np.zeros((0, 0), dtype=np.float32)
    index_dir.mkdir(parents=True, exist_ok=True)
    guides.write_chunks(chunks, index_dir / "chunks.jsonl")
    np.save(index_dir / "vectors.npy", vectors)
    manifest = {"model": config.EMBEDDING_MODEL, "max_seq_length": config.EMBEDDING_MAX_SEQ_LENGTH,
                "count": len(chunks), "dim": int(vectors.shape[1]) if len(chunks) else 0,
                "chunks_sha256": hashlib.sha256((index_dir / "chunks.jsonl").read_bytes()).hexdigest()}
    (index_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with _lock:
        _state = None
    return len(chunks)


def _previous_vectors(index_dir: Path) -> dict[str, np.ndarray]:
    """이전 색인의 (임베딩 텍스트 -> 벡터). 출처 URL·연결 제도만 바뀐 조각은 다시 임베딩하지 않는다 (약 20분 -> 몇 초).
    모델·최대 길이가 다르면 쓰지 않는다."""
    try:
        manifest = json.loads((index_dir / "manifest.json").read_text(encoding="utf-8"))
        if (manifest["model"], manifest["max_seq_length"]) != (config.EMBEDDING_MODEL, config.EMBEDDING_MAX_SEQ_LENGTH):
            return {}
        chunks = guides.read_chunks(index_dir / "chunks.jsonl")
        vectors = np.load(index_dir / "vectors.npy")
    except (OSError, ValueError, KeyError):
        return {}
    if len(chunks) != len(vectors):
        return {}
    return {embed_text(c): v for c, v in zip(chunks, vectors)}


def _load(index_dir: Path) -> dict | None:
    """색인을 읽는다. 없으면 None (검색은 []), 모델이 다르면 오류 (다른 모델의 벡터와 질문 벡터를 비교하지 않게)."""
    global _state
    with _lock:
        if _state is not None and _state["dir"] == Path(index_dir):
            return _state
        manifest_path = Path(index_dir) / "manifest.json"
        if not manifest_path.exists():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["model"] != config.EMBEDDING_MODEL:
            raise RuntimeError(f"판정 문서 색인 모델({manifest['model']})이 현재 모델({config.EMBEDDING_MODEL})과 다릅니다. "
                               "python -m src.index.guide_store 로 다시 만드세요.")
        chunks = guides.read_chunks(Path(index_dir) / "chunks.jsonl")
        vectors = np.load(Path(index_dir) / "vectors.npy")
        from rank_bm25 import BM25Okapi
        bm25 = BM25Okapi([_tokenize(embed_text(c)) for c in chunks]) if chunks else None
        _state = {"dir": Path(index_dir), "chunks": chunks, "vectors": vectors, "bm25": bm25}
        return _state


def _allowed(c: GuideChunk, service_ids: list[str] | None, region: str | None) -> bool:
    """service_ids가 있으면 그 제도의 문서와 공통 기준 문서(service_ids가 빈 문서)만. 전국 문서는 항상,
    지역 문서는 지역이 맞을 때만 (지역을 모르면 지역 문서는 쓰지 않는다)."""
    if service_ids and c.service_ids and not set(service_ids) & set(c.service_ids):
        return False
    if (c.scope or "전국") == "전국":
        return True
    return region is not None and normalize_region(c.scope) == normalize_region(region)


def _rank(scores: np.ndarray, n: int) -> list[int]:
    order = np.argsort(-scores, kind="stable")[:n]
    return [int(i) for i in order if scores[i] > 0]


def search_guides(query: str, service_ids: list[str] | None = None, region: str | None = None,
                  k: int = 3, *, index_dir: Path | None = None) -> list[GuideChunk]:
    """판정 근거 후보 조각 (관련도 순, 최대 k개). 색인이 없거나 맞는 문서가 없으면 []."""
    if not (query or "").strip() or k <= 0:
        return []
    state = _load(Path(index_dir) if index_dir else INDEX_DIR)
    if state is None:
        log.warning("판정 문서 색인이 없습니다 (python -m src.index.guide_store).")
        return []
    chunks = state["chunks"]
    cand = np.asarray([i for i, c in enumerate(chunks) if _allowed(c, service_ids, region)], dtype=int)
    if not len(cand):
        return []
    from src.index import embedder
    q = np.asarray(embedder.embed_query(query), dtype=np.float32)
    vec_scores = state["vectors"][cand] @ (q / max(float(np.linalg.norm(q)), 1e-12))
    bm25_scores = np.asarray(state["bm25"].get_scores(_tokenize(query)), dtype=float)[cand]
    fused: dict[int, float] = {}
    # 코사인 유사도는 0 이하일 수도 있어 1을 더해 순위만 매긴다 (BM25는 0이면 낱말이 하나도 겹치지 않은 것)
    for weight, ranked in ((WEIGHTS[0], _rank(vec_scores + 1.0, CANDIDATES)), (WEIGHTS[1], _rank(bm25_scores, CANDIDATES))):
        for r, i in enumerate(ranked):
            fused[i] = fused.get(i, 0.0) + weight / (config.RRF_K + r + 1)
    best = sorted(fused, key=lambda i: (-fused[i], int(cand[i])))[:k]
    return [chunks[int(cand[i])] for i in best]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="판정 기준 문서 색인 (data/guides/_index)")
    ap.add_argument("--dir", default=str(guides.GUIDES_DIR), help="guide_chunks.jsonl이 있는 폴더")
    ap.add_argument("--index-dir", default=str(INDEX_DIR))
    ap.add_argument("--query", help="색인 후(또는 --no-build) 이 질문으로 검색 결과를 출력")
    ap.add_argument("--service-ids", default="", help="쉼표로 구분한 service_id (검색 범위)")
    ap.add_argument("--no-build", action="store_true", help="색인을 다시 만들지 않고 검색만")
    args = ap.parse_args(argv)
    if not args.no_build:
        n = build(index_dir=Path(args.index_dir), guides_dir=Path(args.dir))
        print(f"판정 문서 조각 {n}개 색인 -> {args.index_dir}")
    if args.query:
        ids = [s for s in args.service_ids.split(",") if s] or None
        for c in search_guides(args.query, ids, k=5, index_dir=Path(args.index_dir)):
            print(f"- {c.doc_title} {c.page}쪽 | {c.heading}\n  {c.text[:120]!r}")


if __name__ == "__main__":
    main()
