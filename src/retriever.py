"""[B] 검색: 조건 필터(정형) 후 sql / bm25 / vector / hybrid 중 한 모드로 검색.

모든 모드가 같은 조건 필터를 먼저 걸고, 결과를 '서비스 단위'로 합쳐 돌려준다.
그래서 평가에서 모드끼리 공정하게 비교할 수 있다.
"""
import re
from functools import lru_cache

from src import db
from src.config import CATEGORY_BONUS, CHROMA_DIR, COLLECTION_NAME, DEFAULT_TOP_K, RRF_K, SEARCH_MODES
from src.regions import NATIONWIDE
from src.schema import Conditions, Evidence, ServiceHit

MAX_EVIDENCE = 2   # 서비스 1건당 답변 근거로 넘길 청크 수

# ---------- 토큰화 ----------
# 형태소 분석기 없이 한국어를 다루기 위해 한글은 글자 2개씩(바이그램)으로 쪼갠다.
# "구직촉진수당" → 구직 직촉 촉진 진수 수당 → "구직수당"으로 물어도 일부가 겹쳐 점수가 난다.
_WORD = re.compile(r"[가-힣]+|[a-zA-Z]+|\d+")
# 긴 어미부터 검사해야 '인데요'가 '요'보다 먼저 떨어진다
_JOSA = ("에서는", "으로는", "에게는", "이라도", "인데요", "이에요",
         "인데", "까지", "부터", "에서", "으로", "에게", "하고", "돼요", "해요", "예요", "라서",
         "은", "는", "이", "가", "을", "를", "에", "의", "도", "로", "와", "과", "요", "돼")
_STOP = {"있나요", "있을까요", "있어", "있는", "받을", "어떻게", "알려주세요", "알려줘", "무엇", "뭐가", "하는데"}


def tokenize(text: str) -> list[str]:
    tokens = []
    for w in _WORD.findall(text.lower()):
        if re.fullmatch(r"[가-힣]+", w) and len(w) >= 2:
            tokens += [w[i:i + 2] for i in range(len(w) - 1)]
        else:
            tokens.append(w)
    return tokens


def keywords(question: str) -> list[str]:
    """[SQL 모드용] 질문 → 조사를 뗀 2글자 이상 단어."""
    out = []
    for w in _WORD.findall(question):
        for j in _JOSA:
            if w.endswith(j) and len(w) - len(j) >= 2:
                w = w[: -len(j)]
                break
        if len(w) >= 2 and w not in _STOP and w not in out:
            out.append(w)
    return out


# ---------- 공통: 청크 순위 → 서비스 순위 ----------
def _to_hits(ranked_chunks: list[dict], k: int) -> list[ServiceHit]:
    """청크 목록(좋은 순) → 서비스 단위로 묶어 상위 k개. 같은 서비스의 청크는 근거로 모은다."""
    order, evidence, scores = [], {}, {}
    for c in ranked_chunks:
        sid = c["service_id"]
        if sid not in evidence:
            order.append(sid)
            evidence[sid] = []
            scores[sid] = c["score"]
        if len(evidence[sid]) < MAX_EVIDENCE and all(e.field != c["field"] for e in evidence[sid]):
            body = c["text"].split("\n", 1)[-1]          # "[서비스명] 항목" 머리줄 제거
            evidence[sid].append(Evidence(c["field"], body))

    rows = db.get_services(order)
    hits, seen = [], set()
    for sid in order:
        r = rows.get(sid)
        if r is None:
            continue
        key = (r["name"], r["agency"])                   # 같은 이름·기관의 중복 서비스 제거
        if key in seen:
            continue
        seen.add(key)
        hits.append(ServiceHit(
            service_id=sid, name=r["name"], agency=r["agency"], region=r["region"], sigungu=r["sigungu"] or "",
            min_age=r["min_age"], max_age=r["max_age"], deadline=r["deadline"],
            how_to_apply=r["how_to_apply"], url=r["url"], category=r["category"] or "",
            score=scores[sid], evidence=evidence[sid],
        ))
        if len(hits) == k:
            break
    return hits


# ---------- 모드 1: SQL ----------
def _search_sql(question: str, cond: Conditions, k: int) -> list[ServiceHit]:
    kws = keywords(question)
    ranked = db.keyword_search(kws, cond, limit=k * 3)
    chunks_by_sid: dict[str, list] = {}
    for c in db.get_chunks([sid for sid, _ in ranked]):
        chunks_by_sid.setdefault(c["service_id"], []).append(c)
    out = []
    for sid, score in ranked:
        # 키워드가 들어 있는 청크를 근거로 우선 배치
        cs = sorted(chunks_by_sid.get(sid, []), key=lambda c: -sum(kw in c["text"] for kw in kws))
        out += [{"service_id": sid, "field": c["field"], "text": c["text"], "score": float(score)} for c in cs]
    return _to_hits(out, k)


# ---------- 모드 2: BM25 ----------
def _bm25_text(chunk_text: str) -> str:
    """'[국민내일배움카드] 지원대상\\n본문' → '국민내일배움카드 본문'

    머리말의 항목 이름(지원대상·신청방법 등)은 모든 서비스에 똑같이 붙어 있어서,
    BM25 점수에 넣으면 질문 속 같은 단어가 '아무 제도'와 일치해 버린다. 서비스명은 남긴다.
    """
    header, _, body = chunk_text.partition("\n")
    name = header[1:header.rfind("]")] if header.startswith("[") and "]" in header else header
    return f"{name} {body}"


@lru_cache(maxsize=1)
def _bm25_index():
    from rank_bm25 import BM25Okapi
    chunks = [dict(c) for c in db.all_chunks()]
    return BM25Okapi([tokenize(_bm25_text(c["text"])) for c in chunks]), chunks


def _rank_bm25(question: str, cond: Conditions, n: int) -> list[dict]:
    bm25, chunks = _bm25_index()
    allowed = db.eligible_ids(cond)
    scores = bm25.get_scores(tokenize(question))
    ranked = sorted(
        (i for i in range(len(chunks)) if chunks[i]["service_id"] in allowed and scores[i] > 0),
        key=lambda i: -scores[i],
    )[:n]
    return [{**chunks[i], "score": float(scores[i])} for i in ranked]


# ---------- 모드 3: Vector ----------
@lru_cache(maxsize=1)
def get_vectorstore():
    from langchain_chroma import Chroma
    from src.embeddings import get_embeddings
    return Chroma(collection_name=COLLECTION_NAME, embedding_function=get_embeddings(),
                  persist_directory=str(CHROMA_DIR))


def chroma_filter(cond: Conditions) -> dict | None:
    """db.condition_sql 과 같은 규칙을 Chroma 메타데이터 필터 문법으로."""
    clauses = []
    if cond.age is not None:
        clauses += [{"min_age": {"$lte": cond.age}}, {"max_age": {"$gte": cond.age}}]
    if cond.region:
        clauses.append({"region": {"$in": [cond.region, NATIONWIDE]}})
    if cond.sigungu:
        clauses.append({"sigungu": {"$in": [cond.sigungu, ""]}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def _rank_vector(question: str, cond: Conditions, n: int) -> list[dict]:
    if not CHROMA_DIR.exists():
        raise RuntimeError(f"벡터DB가 없습니다: {CHROMA_DIR}\n먼저 scripts/build_index.py 를 실행하세요.")
    results = get_vectorstore().similarity_search_with_score(question, k=n, filter=chroma_filter(cond))
    # Chroma 점수는 거리(작을수록 가까움) → 음수로 바꿔 '클수록 좋음'으로 통일
    return [{"service_id": d.metadata["service_id"], "field": d.metadata["field"],
             "text": d.page_content, "chunk_id": d.metadata["chunk_id"], "score": -dist}
            for d, dist in results]


# ---------- 모드 4: Hybrid (RRF) ----------
def _rank_hybrid(question: str, cond: Conditions, n: int) -> list[dict]:
    """BM25 순위와 Vector 순위를 Reciprocal Rank Fusion으로 합친다: score = Σ 1/(RRF_K + 순위)."""
    fused: dict[str, dict] = {}
    for ranked in (_rank_bm25(question, cond, n), _rank_vector(question, cond, n)):
        for rank, c in enumerate(ranked, start=1):
            item = fused.setdefault(c["chunk_id"], {**c, "score": 0.0})
            item["score"] += 1.0 / (RRF_K + rank)
    return sorted(fused.values(), key=lambda c: -c["score"])


def _boost_category(hits: list[ServiceHit], category: str | None, k: int) -> list[ServiceHit]:
    """분야 가산점(소프트): 순위 점수 1/(RRF_K+순위)에 분야가 맞으면 CATEGORY_BONUS를 더해 재정렬.

    하드 필터가 아니라서 분야 분류가 애매한 제도도 순위가 충분히 높으면 그대로 남는다.
    """
    if not category:
        return hits[:k]
    rescored = [(1.0 / (RRF_K + i) + (CATEGORY_BONUS if category in h.category else 0.0), h)
                for i, h in enumerate(hits, start=1)]
    return [h for _, h in sorted(rescored, key=lambda x: -x[0])][:k]


def search(question: str, cond: Conditions, mode: str = "hybrid", k: int = DEFAULT_TOP_K,
           category: str | None = None) -> list[ServiceHit]:
    if mode not in SEARCH_MODES:
        raise ValueError(f"mode는 {SEARCH_MODES} 중 하나여야 합니다: {mode}")
    pool = k * 3 if category else k     # 가산점으로 순위를 바꾸려면 후보를 넉넉히
    if mode == "sql":
        hits = _search_sql(question, cond, pool)
    else:
        ranker = {"bm25": _rank_bm25, "vector": _rank_vector, "hybrid": _rank_hybrid}[mode]
        hits = _to_hits(ranker(question, cond, pool * 8), pool)   # 청크→서비스로 묶이면 줄어들어 8배로 가져옴
    return _boost_category(hits, category, k)


def search_multi(queries: list[str], cond: Conditions, mode: str = "hybrid", k: int = DEFAULT_TOP_K,
                 category: str | None = None) -> list[ServiceHit]:
    """[Multi Query] 원문·재작성 검색어로 각각 검색한 서비스 순위를 RRF로 합친다.

    재작성이 빗나가도(예: 항목 이름이 섞임) 원문 순위가 받쳐 주고,
    원문이 자기소개로 흐려져도 재작성 순위가 받쳐 준다. 두 목록 모두 상위인 제도가 1위가 된다.
    """
    qs = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))   # 빈 값·중복 제거
    if len(qs) == 1:
        return search(qs[0], cond, mode=mode, k=k, category=category)

    pool = k * 3
    fused: dict[str, float] = {}
    first: dict[str, ServiceHit] = {}
    for q in qs:
        for rank, h in enumerate(search(q, cond, mode=mode, k=pool), start=1):
            fused[h.service_id] = fused.get(h.service_id, 0.0) + 1.0 / (RRF_K + rank)
            first.setdefault(h.service_id, h)            # 근거 청크는 먼저 찾은 쪽 것을 사용
    hits = sorted(first.values(), key=lambda h: -fused[h.service_id])
    for h in hits:
        h.score = fused[h.service_id]
    return _boost_category(hits, category, k)
