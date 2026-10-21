"""④ 벡터 저장소 - 조각 + 메타데이터 저장, 벡터/BM25 조회. 오프라인 색인은 문서가 바뀔 때만 실행.

구성
- 벡터 검색: Chroma(영속 저장, cosine). 임베딩은 우리가 만들어 넣는다(Chroma 기본 임베딩 사용 안 함).
- 키워드 검색(BM25): rank_bm25 + 글자 2-gram 토큰화(한국어는 조사가 붙어 단어 단위로 자르면 놓친다).
  BM25 색인은 Chroma에 저장된 조각 텍스트에서 만들어지는 파생물이라, 색인이 바뀌면 자동으로 다시 만든다.
- 임베딩 모델마다 컬렉션을 따로 쓴다(모델을 바꿔도 차원이 달라 깨지지 않음).

소프트 필터를 저장소에서 처리하는 방법
- "값이 비어 있으면 통과"를 쿼리로 표현하기 어려우므로, **저장할 때 빈 값을 통과하는 값으로 바꿔 둔다**.
    지역 없음 -> ""(전국과 같이 통과), 연령 하한 없음 -> -1, 연령 상한 없음 -> 999, 신청기한 없음 -> 99999999
  `build_where(filters)`가 이 규칙에 맞는 Chroma where 조건을 만든다. 읽을 때는 원래대로(키 생략) 되돌린다.
- 필터 없이 검색하는 실험(Vector 단독 등)은 where=None을 넘긴다. build_where는 기한 지난 제도를 항상 제외한다.

실행: python -m src.index.vector_store --sample --query "월세가 부담돼요"
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date
from pathlib import Path

import numpy as np

from src.common import config
from src.common.schema import SECTIONS, Chunk, Filters
from src.index import embedder
from src.retrieve import filters as filter_rules

# 값이 없을 때 저장하는 대체값 (소프트 필터)
NO_AGE_MIN = -1
NO_AGE_MAX = 999
NO_DEADLINE = 99999999
NATIONWIDE = "전국"

_collections: dict[tuple[str, str], object] = {}
_bm25_cache: dict[str, tuple[str, object, list[str]]] = {}


# ---------------------------------------------------------------------------
# 저장소 연결
# ---------------------------------------------------------------------------
def _collection_name() -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", config.EMBEDDING_MODEL).strip("_.-")[:50]
    return f"chunks__{slug}"


def _store_dir() -> Path:
    return Path(config.VECTORSTORE_DIR)


def _collection():
    """영속 컬렉션을 연다 (경로·모델명별로 한 번만)."""
    key = (str(_store_dir()), _collection_name())
    if key not in _collections:
        import chromadb
        from chromadb.config import Settings

        _store_dir().mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=key[0], settings=Settings(anonymized_telemetry=False))
        _collections[key] = client.get_or_create_collection(
            name=key[1], embedding_function=None, configuration={"hnsw": {"space": "cosine"}}
        )
    return _collections[key]


def reset_cache() -> None:
    """열어 둔 연결과 BM25 캐시를 버린다 (테스트용)."""
    _collections.clear()
    _bm25_cache.clear()


def _version_file() -> Path:
    return _store_dir() / "index_version"


def _bump_version() -> None:
    """색인이 바뀌었음을 표시한다. BM25 캐시는 이 값이 바뀌면 다시 만든다 (다른 프로세스가 바꿔도 감지)."""
    _store_dir().mkdir(parents=True, exist_ok=True)
    _version_file().write_text(str(time.time_ns()), encoding="utf-8")


def _read_version() -> str:
    try:
        return _version_file().read_text(encoding="utf-8")
    except FileNotFoundError:
        return "0"


# ---------------------------------------------------------------------------
# Chunk <-> 저장 형식
# ---------------------------------------------------------------------------
def _date_int(iso: str | None) -> int:
    digits = re.sub(r"\D", "", iso or "")
    return int(digits[:8]) if len(digits) >= 8 else NO_DEADLINE


def _to_store_metadata(chunk: Chunk) -> dict:
    m = chunk.metadata or {}
    return {
        "service_id": chunk.service_id,
        "service_name": chunk.service_name,
        "section": chunk.section,
        "detail_url": chunk.detail_url,
        "agency": m.get("agency") or "",
        "field": m.get("field") or "",
        "region": m.get("region") or filter_rules.agency_region(m.get("agency")) or "",   # A가 지역을 못 채우면 기관명으로 추정
        "deadline": m.get("deadline") or "",
        "deadline_int": _date_int(m.get("deadline")),
        "age_min": m["age_min"] if m.get("age_min") is not None else NO_AGE_MIN,
        "age_max": m["age_max"] if m.get("age_max") is not None else NO_AGE_MAX,
        "target_tags": "|".join(m.get("target_tags") or []),
    }


def _chunk_from_store(chunk_id: str, document: str, m: dict) -> Chunk:
    md: dict = {}
    for key in ("agency", "field", "region", "deadline"):
        if m.get(key):
            md[key] = m[key]
    if m.get("age_min", NO_AGE_MIN) != NO_AGE_MIN:
        md["age_min"] = int(m["age_min"])
    if m.get("age_max", NO_AGE_MAX) != NO_AGE_MAX:
        md["age_max"] = int(m["age_max"])
    tags = [t for t in (m.get("target_tags") or "").split("|") if t]
    if tags:
        md["target_tags"] = tags
    return Chunk(
        chunk_id=chunk_id,
        service_id=m["service_id"],
        service_name=m["service_name"],
        section=m["section"],
        text=document,
        metadata=md,  # type: ignore[arg-type]
        detail_url=m.get("detail_url", ""),
    )


def build_where(filters: Filters) -> dict | None:
    """Filters -> Chroma where. 값이 없는 제도는 통과(소프트), 신청기한이 지난 제도는 제외.

    - age: 연령 하한 <= 나이 <= 연령 상한 (한쪽이라도 비어 있으면 그쪽은 통과)
    - region: 사용자 지역, "전국", 지역 값 없음 중 하나
    - field·situation: 필터로 쓰지 않는다 (자격이 아니라 관심사. 분야는 hybrid.search가 가산점으로 반영한다)
    - today: 이 날짜 이후까지 신청 가능한 제도만 (없으면 오늘). situation은 필터로 쓰지 않는다(질문 문장에 이미 담겨 검색에 반영됨).
    """
    conds: list[dict] = []
    age = filters.get("age")
    if age is not None:
        conds += [{"age_min": {"$lte": age}}, {"age_max": {"$gte": age}}]
    if filters.get("region"):
        conds.append({"region": {"$in": [filters["region"], NATIONWIDE, ""]}})
    conds.append({"deadline_int": {"$gte": _date_int(filters.get("today") or date.today().isoformat())}})
    return conds[0] if len(conds) == 1 else {"$and": conds}


# ---------------------------------------------------------------------------
# 색인 (오프라인)
# ---------------------------------------------------------------------------
def build_index(chunks: list[Chunk], batch_size: int = config.INDEX_BATCH_SIZE) -> None:
    """조각을 임베딩해 저장한다. chunk_id 기준 upsert라 변경분만 넣어도 된다.

    넘어온 서비스의 예전 조각 중 이번에 없는 것(항목이 사라진 경우)은 지운다.
    """
    unique = {c.chunk_id: c for c in chunks}          # 같은 chunk_id가 겹치면 마지막 것
    if not unique:
        return
    col = _collection()

    by_service: dict[str, set[str]] = {}
    for c in unique.values():
        by_service.setdefault(c.service_id, set()).add(c.chunk_id)
    service_ids = list(by_service)
    for i in range(0, len(service_ids), 200):
        batch = service_ids[i : i + 200]
        old = col.get(where={"service_id": {"$in": batch}}, include=[])["ids"]
        stale = [cid for cid in old if cid not in unique]
        if stale:
            col.delete(ids=stale)

    items = list(unique.values())
    for i in range(0, len(items), batch_size):
        part = items[i : i + batch_size]
        col.upsert(
            ids=[c.chunk_id for c in part],
            embeddings=embedder.embed_texts([c.text for c in part], kind="passage"),
            documents=[c.text for c in part],
            metadatas=[_to_store_metadata(c) for c in part],
        )
    _bump_version()


def remove_services(service_ids: list[str]) -> None:
    """종료·삭제된 서비스의 조각을 제거."""
    ids = list(service_ids)
    for i in range(0, len(ids), 200):
        _collection().delete(where={"service_id": {"$in": ids[i : i + 200]}})
    if ids:
        _bump_version()


def clear_index() -> None:
    """전체 재색인(--full) 전에 컬렉션을 비운다."""
    import chromadb
    from chromadb.config import Settings

    name = _collection_name()
    client = chromadb.PersistentClient(path=str(_store_dir()), settings=Settings(anonymized_telemetry=False))
    try:
        client.delete_collection(name)
    except Exception:  # 컬렉션이 아직 없으면 무시
        pass
    reset_cache()
    _bump_version()


def count() -> int:
    return _collection().count()


# ---------------------------------------------------------------------------
# 조회 (온라인)
# ---------------------------------------------------------------------------
def vector_search(query: str, k: int, where: dict | None = None) -> list[tuple[Chunk, float]]:
    """의미 검색. 점수는 코사인 유사도(높을수록 가까움)."""
    col = _collection()
    n = col.count()
    if n == 0 or k <= 0:
        return []
    res = col.query(
        query_embeddings=[embedder.embed_query(query)],
        n_results=min(k, n),
        where=where,
        include=["documents", "metadatas", "distances"],
    )
    return [
        (_chunk_from_store(cid, doc, meta), 1.0 - float(dist))
        for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])
    ]


def _tokenize(text: str) -> list[str]:
    """글자 2-gram. '청년월세를' -> 청년 년월 월세 세를. 한 글자 단어는 그대로."""
    tokens: list[str] = []
    for word in re.findall(r"[0-9A-Za-z가-힣]+", text.lower()):
        if len(word) == 1:
            tokens.append(word)
        else:
            tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
    return tokens


def _bm25_index():
    """(BM25, 조각 id 목록). 색인 버전이 바뀌었을 때만 Chroma의 텍스트로 다시 만든다."""
    key, version = str(_store_dir()) + "|" + _collection_name(), _read_version()
    cached = _bm25_cache.get(key)
    if cached and cached[0] == version:
        return cached[1], cached[2]
    from rank_bm25 import BM25Okapi

    data = _collection().get(include=["documents"])
    ids = list(data["ids"])
    bm25 = BM25Okapi([_tokenize(d) for d in data["documents"]]) if ids else None
    _bm25_cache[key] = (version, bm25, ids)
    return bm25, ids


def bm25_search(query: str, k: int, where: dict | None = None) -> list[tuple[Chunk, float]]:
    """제도명·소관기관·지역명 같은 정확한 일치를 위한 키워드 검색. 점수는 BM25(높을수록 관련)."""
    tokens = _tokenize(query)
    bm25, ids = _bm25_index()
    if not tokens or bm25 is None or k <= 0:
        return []
    scores = np.asarray(bm25.get_scores(tokens), dtype=float)
    if where is not None:
        allowed = set(_collection().get(where=where, include=[])["ids"])
        scores = np.where([cid in allowed for cid in ids], scores, 0.0)
    top = [i for i in np.argsort(-scores)[:k] if scores[i] > 0]
    if not top:
        return []
    top_ids = [ids[i] for i in top]
    data = _collection().get(ids=top_ids, include=["documents", "metadatas"])
    rows = {cid: (doc, meta) for cid, doc, meta in zip(data["ids"], data["documents"], data["metadatas"])}
    return [(_chunk_from_store(cid, *rows[cid]), float(scores[i])) for cid, i in zip(top_ids, top)]


def get_service_chunks(service_id: str) -> list[Chunk]:
    """한 서비스의 모든 조각(항목 순). 검색 결과에 없던 다른 항목(선정기준·신청방법 등)이 필요할 때 쓴다."""
    data = _collection().get(where={"service_id": service_id}, include=["documents", "metadatas"])
    chunks = [_chunk_from_store(cid, doc, m) for cid, doc, m in zip(data["ids"], data["documents"], data["metadatas"])]
    order = {s: i for i, s in enumerate(SECTIONS)}
    return sorted(chunks, key=lambda c: (order.get(c.section, len(order)), c.chunk_id))


# ---------------------------------------------------------------------------
# 명령줄: 색인 + 검색 확인
# ---------------------------------------------------------------------------
def _load_sample_chunks() -> list[Chunk]:
    path = config.ROOT / "tests" / "fixtures" / "sample_chunks.json"
    return [Chunk(**c) for c in json.loads(path.read_text(encoding="utf-8"))["chunks"]]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="벡터 저장소 색인·확인")
    ap.add_argument("--sample", action="store_true", help="A의 결과 대신 tests/fixtures 샘플 조각을 색인")
    ap.add_argument("--full", action="store_true", help="색인을 비우고 전체를 다시 만든다")
    ap.add_argument("--query", help="색인 후 이 질문으로 벡터·BM25 검색 결과를 출력")
    ap.add_argument("--k", type=int, default=config.DEFAULT_TOP_K)
    args = ap.parse_args(argv)

    if args.full:
        clear_index()
    if args.sample:
        chunks = _load_sample_chunks()
    else:
        from src.ingest import pipeline

        try:
            chunks = pipeline.run(full=args.full)
        except NotImplementedError:
            raise SystemExit("ingest.pipeline이 아직 구현되지 않았습니다. --sample로 샘플 조각을 색인해 보세요.")
    build_index(chunks)
    print(f"[index] 조각 {len(chunks)}개 색인 / 총 {count()}개 ({_collection_name()})")

    if args.query:
        for name, fn in (("벡터", vector_search), ("BM25", bm25_search)):
            print(f"\n[{name}] {args.query!r}")
            for chunk, score in fn(args.query, args.k):
                print(f"  {score:7.3f}  {chunk.chunk_id}  {chunk.service_name}")


if __name__ == "__main__":
    main()
