"""[C] 검색된 제도의 원문만 근거로 답변 생성 (마크다운 카드 형식 + 인용)."""
from collections.abc import Iterator

from langchain_core.prompts import ChatPromptTemplate

from src import db
from src.config import CHAT_MODEL, OPENAI_API_KEY, require
from src.schema import Conditions, ServiceHit, age_text

FIELD_LIMIT = 1200   # 항목 하나당 LLM에 넘길 최대 글자 수 (아주 긴 선정기준 대비)

SYSTEM = """너는 정부 지원제도 안내 도우미다. [제도 목록]의 원문만 근거로 답한다. 목록에 없는 내용은 절대 지어내지 않는다.

## 답변 순서
1. 첫 줄: 결론 한 문장. 예) "조건에 맞을 수 있는 제도를 3개 찾았어요."
   - 질문이 특정 제도 하나(예: 국민내일배움카드)에 대한 것이면 그 제도만 자세히 설명한다.
2. 제도별 카드 (질문과 관련 있는 제도만, 관련도 높은 순, 최대 5개)
3. 마지막 줄: "> 최종 자격과 금액은 담당 기관에서 꼭 확인하세요."

## 제도 카드 형식 (마크다운 그대로)
### {{번호}}. {{서비스명}}
**{{소관기관}}** · {{지역}} · {{연령}} · [상세 보기]({{링크}})

{{요약 한 문장}}

**해당 여부** {{아래 셋 중 하나}} — {{이유 한 문장}}
- ✅ 해당 가능성 높음: 사용자 조건으로 확인되는 자격이 모두 맞을 때
- ⚠️ 조건 확인 필요: 소득·재산·가구·재학 여부 등 사용자 정보로 판단할 수 없는 조건이 있을 때
- ❌ 해당 어려움: 원문 조건과 사용자 조건이 명확히 어긋날 때 (이 경우 카드는 짧게)

**무엇을 받나요**
- 지원내용을 항목별로 정리 (금액·기간·횟수 등 숫자는 원문 그대로)

**누가 받을 수 있나요**
- 지원대상·선정기준을 항목별로 정리 (원문의 조건을 빠뜨리지 말고 쉬운 말로)

**확인이 필요한 조건**
- 사용자가 직접 확인해야 할 조건만. 없으면 이 항목은 통째로 생략한다. "확인 필요"라고만 쓰지 않는다.

**신청 방법**
- 방법: … / 기한: … / 접수: … / 문의: … (정보가 없는 항목은 생략)

<sub>근거: "원문에서 그대로 옮긴 짧은 문구" [번호]</sub>

## 규칙
- 질문이 원하는 지원(예: 취업)과 관련 없는 제도는 대상만 비슷해도 넣지 않는다.
- 제도 지역이 시·군·구(예: 서울특별시 강남구)인데 사용자의 시·군·구를 모르면 '강남구 거주자 대상'을 확인이 필요한 조건에 넣는다.
- 신청기한이 지났을 수 있으면 그렇게 알리고, 기한 정보가 없으면 쓰지 않는다.
- 관련 제도가 하나도 없으면 지어내지 말고 "관련 제도를 찾지 못했어요"라고 한 뒤 정부24(혜택알리미)·복지로 확인을 안내한다.
- 행정 용어는 쉬운 말로 풀되, 금액·나이·기간 같은 숫자는 바꾸지 않는다."""

PROMPT = ChatPromptTemplate.from_messages([
    ("system", SYSTEM),
    ("human", "[사용자 조건] {conditions}\n[질문] {question}\n\n[제도 목록]\n{context}"),
])


def describe(cond: Conditions) -> str:
    place = " ".join(p for p in (cond.region, cond.sigungu) if p)
    parts = [f"나이 {cond.age}세" if cond.age is not None else "나이 모름",
             f"거주지 {place}" if place else "거주지 모름"]
    return ", ".join(parts)


def place_text(h: ServiceHit) -> str:
    return f"{h.region} {h.sigungu}".strip()


def _clip(text: str | None) -> str:
    text = (text or "").strip()
    return text if len(text) <= FIELD_LIMIT else text[:FIELD_LIMIT] + " …(이하 생략, 상세 링크 참고)"


def format_context(hits: list[ServiceHit]) -> str:
    """검색에 걸린 조각만이 아니라 DB의 원문 항목 전체를 넘겨야 LLM이 조건을 빠짐없이 설명할 수 있다."""
    rows = db.get_services([h.service_id for h in hits])
    blocks = []
    for i, h in enumerate(hits, start=1):
        r = rows.get(h.service_id)
        fields = [
            ("요약", r["summary"] if r else ""),
            ("지원대상", r["target"] if r else ""),
            ("선정기준", r["criteria"] if r else ""),
            ("지원내용", r["content"] if r else ""),
            ("신청방법", h.how_to_apply),
            ("신청기한", h.deadline),
            ("접수기관", r["reception"] if r else ""),
            ("문의", r["contact"] if r else ""),
        ]
        body = "\n".join(f"<{label}>\n{_clip(v)}" for label, v in fields if (v or "").strip())
        blocks.append(
            f"[{i}] {h.name}\n소관기관: {h.agency} | 지역: {place_text(h)} | "
            f"연령: {age_text(h.min_age, h.max_age)} | 링크: {h.url}\n{body}"
        )
    return "\n\n---\n\n".join(blocks)


NO_RESULT = ("입력하신 조건에 맞는 제도를 찾지 못했어요. 질문을 바꾸거나 나이·거주지를 확인해 주세요.\n\n"
             "> 정부24(혜택알리미)나 복지로에서도 확인할 수 있어요.")


def _chain():
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(model=CHAT_MODEL, temperature=0, api_key=require("OPENAI_API_KEY", OPENAI_API_KEY))
    return PROMPT | llm


def _inputs(question: str, cond: Conditions, hits: list[ServiceHit]) -> dict:
    return {"conditions": describe(cond), "question": question, "context": format_context(hits)}


def generate(question: str, cond: Conditions, hits: list[ServiceHit]) -> str:
    """답변 전체를 한 번에 반환 (평가 스크립트 등에서 사용)."""
    if not hits:
        return NO_RESULT
    return _chain().invoke(_inputs(question, cond, hits)).content


def generate_stream(question: str, cond: Conditions, hits: list[ServiceHit]) -> Iterator[str]:
    """답변을 글자 단위로 흘려보냄 → 화면에 바로 써지기 시작해 긴 답변도 덜 기다리게 느껴진다."""
    if not hits:
        yield NO_RESULT
        return
    for chunk in _chain().stream(_inputs(question, cond, hits)):
        if chunk.content:
            yield chunk.content
