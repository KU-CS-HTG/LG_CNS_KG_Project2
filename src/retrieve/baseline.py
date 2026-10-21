"""Baseline 검색 (SQL + 키워드). 실험 비교용이며 서비스 경로에서는 쓰지 않는다 (기획서 6.2, 9.4).

벡터·BM25 색인 없이 A의 SQLite 저장소(`store.sql_keyword_search`)만 쓴다.
질문에서 키워드를 뽑아(형태소 분석 없이 조사만 떼는 단순 방식) 하나라도 들어간 제도를 가져온 뒤,
들어간 키워드 종류가 많은 순(서비스명에 있으면 가중)으로 줄 세워 Top-k를 돌려준다.
정형 필터(나이·지역·기한)는 쓰지 않는다. 필터는 hybrid_filter 실험이 더하는 요소이기 때문이다.
"""
from __future__ import annotations

import re

from src.common import config
from src.common.schema import Chunk
from src.ingest import store
from src.ingest.chunker import chunk_service

CANDIDATE_LIMIT = 2000        # SQL이 ID 순으로 자르므로 줄 세우기 전에 충분히 가져온다
NAME_WEIGHT = 3               # 서비스명에 키워드가 있으면 본문보다 가중

# 질문 어미·일반어: 어떤 제도 본문에나 있어 변별력이 없다
STOPWORDS = frozenset("""
있나요 있을까요 있는지 있어요 있는 없나요 받을 받고 받아 받나요 알려 알려주세요 알려줘 주세요 해주세요 해요 하는 하고 하면
어떻게 무엇 뭐가 어떤 얼마 언제 어디 제도 지원 지원금 정부 사람 경우 때문 정도 가능 관련 대한 위한 통해 같은 대한민국
저는 제가 나는 내가 우리 그리고 하지만 그래서 너무 매우 아직 이미 곧 혼자 다음
""".split())

# 긴 것부터 떼어야 한다 ('에서' 전에 '서' 등)
PARTICLES = ("에서는", "으로는", "에서", "에게", "으로", "까지", "부터", "인데", "이고", "이라", "해서", "한데", "는데",
             "은", "는", "이", "가", "을", "를", "에", "의", "도", "로", "과", "와", "만")


def extract_keywords(query: str) -> list[str]:
    """질문에서 키워드(2글자 이상, 조사 제거, 불용어 제외, 중복 제거, 등장 순서 유지)를 뽑는다."""
    keywords: list[str] = []
    for token in re.findall(r"[0-9A-Za-z가-힣]+", query):
        if token.isdigit():
            continue
        for particle in PARTICLES:
            if token.endswith(particle) and len(token) - len(particle) >= 2:
                token = token[: -len(particle)]
                break
        if len(token) >= 2 and token not in STOPWORDS and token not in keywords:
            keywords.append(token)
    return keywords


def _score(service: dict, keywords: list[str]) -> tuple[int, int]:
    name = (service.get("service_name") or "").lower()
    body = " ".join(str(service.get(f) or "") for f in
                    ("agency", "support_target", "selection_criteria", "support_content", "how_to_apply")).lower()
    in_name = sum(1 for kw in keywords if kw.lower() in name)
    in_body = sum(1 for kw in keywords if kw.lower() in body)
    return (in_name * NAME_WEIGHT + in_body, in_name)


def _best_chunk(service: dict, keywords: list[str]) -> Chunk | None:
    chunks = chunk_service(service)
    if not chunks:
        return None
    return max(chunks, key=lambda c: sum(1 for kw in keywords if kw.lower() in c.text.lower()))   # 동점이면 앞 조각


def search(query: str, filters: dict | None = None, k: int = config.DEFAULT_TOP_K, *, storage=None) -> list[Chunk]:
    """키워드 일치 순 Top-k. 서비스당 조각 1개. `filters`는 받기만 하고 쓰지 않는다 (hybrid.search와 같은 호출 모양)."""
    keywords = extract_keywords(query)
    if not keywords or k <= 0:
        return []
    services = store.sql_keyword_search(keywords, CANDIDATE_LIMIT, storage=storage)
    ranked = sorted(services, key=lambda s: (-_score(s, keywords)[0], -_score(s, keywords)[1], s["service_id"]))
    results: list[Chunk] = []
    for service in ranked:
        chunk = _best_chunk(service, keywords)
        if chunk is not None:
            results.append(chunk)
        if len(results) >= k:
            break
    return results


def active_count(*, storage=None) -> int:
    """검색 대상이 되는 서비스 수. DB 파일이 없으면 만들지 않고 0을 돌려준다 (평가 전에 빈 DB로 돌리는 실수 방지)."""
    from src.ingest.storage import resolve_storage

    paths = storage.validated() if storage is not None else resolve_storage()
    if not paths.db_path.exists():
        return 0
    with store.connection(storage=storage) as con:
        return con.execute("SELECT COUNT(*) FROM services WHERE status='active'").fetchone()[0]
