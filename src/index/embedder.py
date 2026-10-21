"""③ 임베딩 - 로컬 다국어 모델 (bge-m3 우선, 자원 부족 시 multilingual-e5). 후보는 소규모 비교 후 확정.

- 모델은 처음 부를 때 한 번만 불러온다(지연 로딩). 처음에는 모델 파일을 내려받으므로 시간이 걸린다
  (bge-m3 약 2.2GB, Hugging Face 캐시에 저장되어 다음부터는 재사용).
- 벡터는 항상 L2 정규화된다. 따라서 내적 = 코사인 유사도이고, 벡터 저장소는 내적/코사인 어느 쪽으로 검색해도 된다.
- multilingual-e5 계열은 질문에 "query: ", 문서에 "passage: " 접두가 필요하다. 이 모듈이 모델명을 보고 알아서 붙이므로
  호출하는 쪽은 `kind`만 맞게 넘기면 된다 (bge-m3는 접두가 필요 없다).

사용:
    embed_texts(["청년월세 지원(서울)\\n서울에 거주하는 ..."])        # 조각(문서) 임베딩 -> list[list[float]]
    embed_query("월세가 부담돼요")                                   # 질문 임베딩 -> list[float]
스모크 테스트: python -m src.index.embedder
"""
from __future__ import annotations

from typing import Literal

from src.common import config

Kind = Literal["passage", "query"]

_model = None
_model_name: str | None = None
_cache = None
_cache_source: str | None = None
cache_stats = {"hits": 0, "misses": 0}      # 미리 계산한 벡터를 쓴 수 / 모델로 계산한 수 (embed_texts 누적)


def _get_cache():
    """config.EMBEDDING_CACHE의 미리 계산한 벡터를 지연 로딩한다. 경로가 바뀌면 다시 불러온다. 설정이 없으면 None."""
    global _cache, _cache_source
    source = str(config.EMBEDDING_CACHE) if config.EMBEDDING_CACHE else None
    if source is None:
        _cache, _cache_source = None, None
        return None
    if _cache is None or _cache_source != source:
        from src.index import embed_cache

        cache = embed_cache.VectorCache.load_many(source)
        cache.check_model(config.EMBEDDING_MODEL, config.EMBEDDING_MAX_SEQ_LENGTH)   # 모델·길이가 다르면 섞이지 않게 거부
        _cache, _cache_source = cache, source
    return _cache


def _get_model():
    """config.EMBEDDING_MODEL을 지연 로딩한다. 모델명이 바뀌면 다시 불러온다."""
    global _model, _model_name
    if _model is None or _model_name != config.EMBEDDING_MODEL:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:  # pragma: no cover - 설치 안내
            raise ImportError("sentence-transformers가 필요합니다: pip install -r requirements.txt") from e
        model = SentenceTransformer(config.EMBEDDING_MODEL, device=config.EMBEDDING_DEVICE)
        model.max_seq_length = config.EMBEDDING_MAX_SEQ_LENGTH
        _model, _model_name = model, config.EMBEDDING_MODEL
    return _model


def _is_e5() -> bool:
    return "e5" in config.EMBEDDING_MODEL.lower()


def _with_prefix(texts: list[str], kind: Kind) -> list[str]:
    if not _is_e5():
        return texts
    prefix = "query: " if kind == "query" else "passage: "
    return [prefix + t for t in texts]


def embed_texts(texts: list[str], kind: Kind = "passage") -> list[list[float]]:
    """텍스트 N개 -> 같은 순서의 벡터 N개 (L2 정규화). 빈 입력이면 빈 리스트.

    kind: "passage"(색인할 조각, 기본값) 또는 "query"(사용자 질문). 접두가 필요한 모델에서만 차이가 난다.
    """
    if kind not in ("passage", "query"):
        raise ValueError(f"kind는 'passage' 또는 'query'여야 합니다: {kind!r}")
    if not texts:
        return []
    texts = list(texts)
    cache = _get_cache()
    result: list[list[float] | None] = [None] * len(texts)
    todo = list(range(len(texts)))
    if cache is not None:                      # 미리 계산한 벡터가 있는 텍스트는 모델을 부르지 않는다
        from src.index import embed_cache

        todo = []
        for i, text in enumerate(texts):
            row = cache.get(embed_cache.text_key(config.EMBEDDING_MODEL, kind, text))
            if row is None:
                todo.append(i)
            else:
                result[i] = row.tolist()
        cache_stats["hits"] += len(texts) - len(todo)
    cache_stats["misses"] += len(todo)
    if todo:
        vectors = _get_model().encode(
            _with_prefix([texts[i] for i in todo], kind),
            batch_size=config.EMBEDDING_BATCH_SIZE,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=len(todo) > 500,
        )
        for i, vector in zip(todo, vectors.tolist()):
            result[i] = vector
    return result  # type: ignore[return-value]


def embed_query(text: str) -> list[float]:
    """사용자 질문 1개 -> 벡터 1개."""
    return embed_texts([text], kind="query")[0]


def embedding_dim() -> int:
    """벡터 차원 (bge-m3는 1024). 벡터 저장소 컬렉션을 만들 때 쓴다."""
    model = _get_model()
    # sentence-transformers 버전에 따라 메서드 이름이 다르다 (최신은 get_embedding_dimension)
    getter = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    return int(getter())


if __name__ == "__main__":
    import math

    docs = ["청년월세 지원(서울)\n서울에 거주하며 월세로 사는 만 19~34세 무주택 청년", "기초연금\n만 65세 이상 어르신"]
    q = "월세가 부담돼요"
    d_vecs, q_vec = embed_texts(docs), embed_query(q)
    cos = lambda a, b: sum(x * y for x, y in zip(a, b))  # 정규화되어 있으므로 내적 = 코사인
    print(f"모델: {config.EMBEDDING_MODEL}, 차원: {embedding_dim()}, 벡터 길이: {len(q_vec)}")
    print(f"노름(1이어야 함): {math.sqrt(cos(q_vec, q_vec)):.4f}")
    for doc, v in zip(docs, d_vecs):
        print(f"  유사도 {cos(q_vec, v):.3f}  <-  {doc.splitlines()[0]}")
    print("기대: 청년월세 지원이 기초연금보다 높아야 합니다.")
