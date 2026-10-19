"""[C] 질문에서 사용자 조건(나이·거주지) 추출 + 사이드바 값으로 보정."""
from pydantic import BaseModel, Field

from src.config import CHAT_MODEL, OPENAI_API_KEY, require
from src.regions import normalize_region
from src.schema import Conditions


class _Extracted(BaseModel):
    age: int | None = Field(None, description="만 나이. 질문에 없으면 null")
    region: str | None = Field(None, description="거주 시·도 (예: 서울, 경기도). 질문에 없으면 null")


_SYSTEM = (
    "사용자 질문에서 정부 지원제도 자격 판단에 쓸 조건만 뽑는다. "
    "질문에 명시된 것만 채우고, 추측하지 말고 없으면 null로 둔다. "
    "'20대 후반'처럼 범위만 있으면 나이는 null로 둔다."
)


def extract(question: str) -> Conditions:
    """LLM으로 조건 추출. 실패하면 빈 조건(=필터 없음)을 돌려줘 검색은 계속 되게 한다."""
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(model=CHAT_MODEL, temperature=0, api_key=require("OPENAI_API_KEY", OPENAI_API_KEY))
    try:
        result = llm.with_structured_output(_Extracted).invoke(
            [("system", _SYSTEM), ("human", question)]
        )
    except Exception as e:  # 네트워크·파싱 오류
        print(f"[conditions] 조건 추출 실패, 필터 없이 진행: {e}")
        return Conditions()
    age = result.age if result.age is not None and 0 <= result.age <= 120 else None
    return Conditions(age=age, region=normalize_region(result.region))


def merge(extracted: Conditions, override_age: int | None = None,
          override_region: str | None = None) -> Conditions:
    """사이드바에 값을 넣었으면 그 값이 질문에서 뽑은 값보다 우선한다."""
    return Conditions(
        age=override_age if override_age is not None else extracted.age,
        region=normalize_region(override_region) or extracted.region,
    )
