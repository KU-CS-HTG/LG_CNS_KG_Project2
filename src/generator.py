"""[C] 검색된 제도의 근거 문구만 사용해 답변 생성 (인용 포함)."""
from langchain_core.prompts import ChatPromptTemplate

from src.config import CHAT_MODEL, OPENAI_API_KEY, require
from src.schema import Conditions, ServiceHit, age_text

PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "너는 정부 지원제도 안내 도우미다. 반드시 [제도 목록]의 근거 문구만 사용해 답한다.\n"
     "규칙:\n"
     "0. [제도 목록] 중 질문이 원하는 지원(예: 취업)과 관련 없는 제도는 대상만 비슷해도 답변에서 뺀다.\n"
     "1. 제도마다 다음 순서로 쓴다: **서비스명**(소관기관) / 해당 가능 이유 / 지원내용 / "
     "신청방법·신청기한 / 확인 필요 조건 / 근거: \"원문 문구\" [번호] / 상세 링크.\n"
     "2. [번호]는 [제도 목록]의 번호다. 근거 문구는 목록에 있는 표현을 그대로 짧게 인용한다.\n"
     "3. 소득·재산·가구 구성처럼 사용자 정보로 판단할 수 없는 조건은 '확인 필요'로 적는다.\n"
     "4. 신청기한이 지났을 수 있으면 그렇게 알리고, 기한 정보가 없으면 '기한 정보 없음'이라 쓴다.\n"
     "5. 목록에 질문과 관련된 제도가 없으면 지어내지 말고 '관련 근거를 찾지 못했다'고 답한 뒤 "
     "정부24·복지로 확인을 안내한다.\n"
     "6. 마지막 줄: '최종 자격은 담당 기관에서 확인하세요.'"),
    ("human",
     "[사용자 조건] {conditions}\n[질문] {question}\n\n[제도 목록]\n{context}"),
])


def describe(cond: Conditions) -> str:
    parts = [f"나이 {cond.age}세" if cond.age is not None else "나이 모름",
             f"거주지 {cond.region}" if cond.region else "거주지 모름"]
    return ", ".join(parts)


def format_context(hits: list[ServiceHit]) -> str:
    blocks = []
    for i, h in enumerate(hits, start=1):
        evidence = "\n".join(f"- ({e.field}) {e.text}" for e in h.evidence)
        age = age_text(h.min_age, h.max_age)
        blocks.append(
            f"[{i}] {h.name} | 소관기관: {h.agency} | 지역: {h.region} | 연령: {age}\n"
            f"신청방법: {h.how_to_apply or '정보 없음'} | 신청기한: {h.deadline or '정보 없음'}\n"
            f"근거:\n{evidence}\n링크: {h.url}"
        )
    return "\n\n".join(blocks)


def generate(question: str, cond: Conditions, hits: list[ServiceHit]) -> str:
    if not hits:
        return ("입력하신 조건에 맞는 제도를 찾지 못했습니다. 질문을 바꾸거나 나이·거주지를 확인해 주세요.\n"
                "정부24(혜택알리미)나 복지로에서도 확인할 수 있습니다.")
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(model=CHAT_MODEL, temperature=0, api_key=require("OPENAI_API_KEY", OPENAI_API_KEY))
    result = (PROMPT | llm).invoke({
        "conditions": describe(cond), "question": question, "context": format_context(hits),
    })
    return result.content
