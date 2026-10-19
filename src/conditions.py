"""[C] 질문 분석: 조건(나이·거주지) 추출 + 검색어 재작성(Query Rewrite) + 관심 분야 분류."""
from pydantic import BaseModel, Field

from src import db
from src.config import CHAT_MODEL, OPENAI_API_KEY, require
from src.regions import normalize_region, normalize_sigungu
from src.schema import Conditions, QueryPlan


class _Extracted(BaseModel):
    age: int | None = Field(None, description="만 나이. 질문에 없으면 null")
    region: str | None = Field(None, description="거주 시·도 (예: 서울, 경기도). 질문에 없으면 null")
    sigungu: str | None = Field(None, description="거주 시·군·구 (예: 노원구, 수원시). 질문에 없으면 null")
    search_query: str = Field(
        description="지원제도 검색어. 나이·지역·신분 같은 자기소개는 빼고, 원하는 지원 내용만 "
                    "행정 용어로 풀어 쓴다. 예: '취업 지원 구직활동 직업훈련 일자리'")
    category: str | None = Field(None, description="[분야 목록] 중 가장 가까운 하나. 애매하면 null")


_SYSTEM = (
    "사용자 질문을 정부 지원제도 검색용으로 분석한다.\n"
    "- age, region, sigungu: 질문에 명시된 것만 채운다. 추측하지 않는다. "
    "'20대 후반'처럼 범위면 age는 null. '노원구'만 있으면 region은 null로 두지 말고 아는 시·도를 채운다.\n"
    "- search_query: 사용자가 '무엇을 원하는지'만 남긴다. '서울 사는 24살 대학생' 같은 자기소개는 "
    "검색어에 넣지 않는다(조건은 따로 필터링된다). 일상 표현은 제도에 쓰이는 행정 용어로 바꾸고 "
    "관련 용어를 2~5개 덧붙인다. 예) '월세가 부담돼요' → '월세 지원 주거비 임차료 주거급여'\n"
    "  단, 질문에 제도 이름(예: 근로장려금, 국민내일배움카드)이 있으면 그 이름을 바꾸거나 빼지 말고 "
    "검색어 맨 앞에 그대로 둔다. 예) '근로장려금 언제 신청해?' → '근로장려금 신청방법 신청기한'\n"
    "- category: 아래 [분야 목록] 중에서만 고른다.\n\n[분야 목록]\n{categories}"
)


def analyze(question: str) -> QueryPlan:
    """실패하면 원문 질문을 그대로 검색어로 써서 검색은 계속 되게 한다."""
    from langchain_openai import ChatOpenAI

    fallback = QueryPlan(Conditions(), search_query=question)
    try:
        cats = db.categories()
    except RuntimeError:
        cats = []
    llm = ChatOpenAI(model=CHAT_MODEL, temperature=0, api_key=require("OPENAI_API_KEY", OPENAI_API_KEY))
    try:
        r = llm.with_structured_output(_Extracted).invoke([
            ("system", _SYSTEM.format(categories=", ".join(cats) or "(없음)")),
            ("human", question),
        ])
    except Exception as e:  # 네트워크·파싱 오류
        print(f"[conditions] 질문 분석 실패, 원문으로 검색: {e}")
        return fallback

    age = r.age if r.age is not None and 0 <= r.age <= 120 else None
    category = r.category if r.category in cats else None       # 목록 밖 값은 버림
    query = r.search_query.strip() or question
    cond = Conditions(age=age, region=normalize_region(r.region), sigungu=normalize_sigungu(r.sigungu))
    return QueryPlan(cond, query, category)


def merge(extracted: Conditions, override_age: int | None = None,
          override_region: str | None = None, override_sigungu: str | None = None) -> Conditions:
    """사이드바에 값을 넣었으면 그 값이 질문에서 뽑은 값보다 우선한다.

    사이드바에서 시·도를 질문과 다르게 바꿨다면, 질문에서 뽑은 구는 다른 시·도의 구일 수 있어 버린다.
    """
    region = normalize_region(override_region) or extracted.region
    sigungu = normalize_sigungu(override_sigungu)
    if sigungu is None and region == extracted.region:
        sigungu = extracted.sigungu
    return Conditions(
        age=override_age if override_age is not None else extracted.age,
        region=region,
        sigungu=sigungu,
    )
