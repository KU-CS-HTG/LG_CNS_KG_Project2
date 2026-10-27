"""이어진 질문을 판정 기준 문서로 답할지 LLM이 판단한다 (X1 답변 연결).

추천 카드가 나온 뒤의 이어진 질문 중 '같은 가구인가', '이 소득도 들어가나'처럼 안내받은 제도의 자격 기준을
어떻게 해석하는지 묻는 질문은 보조금24의 짧은 선정기준으로는 답할 수 없고 판정 기준 문서(고시·사업안내)에 있다.
그런 질문은 버튼을 누르지 않아도 guide_evidence.answer_guide_question으로 보낸다. 나머지(정보 덧붙이기·다른 도움
찾기)는 지금처럼 질문을 합쳐 다시 추천한다. 판단에 실패하면 다시 추천하는 쪽으로 둔다 (지금 동작과 같다).
LLM 호출은 condition_extractor._chat_json (같은 질문이면 llm_cache로 같은 판단).
프롬프트에 예시 문장을 넣지 않는다 (condition_extractor 머리말: 예시를 그대로 따라 함).
"""
from __future__ import annotations

import logging

from src.common.schema import Recommendation
from src.generate import condition_extractor

log = logging.getLogger(__name__)

PROMPT = """너는 정부 지원제도 상담에서, 제도를 안내받은 사용자가 이어서 한 질문의 종류를 고르는 도구다.
JSON 객체 하나만 출력한다: {"kind": "guide" 또는 "search"}

- guide: 안내받은 제도의 자격·선정 기준을 어떻게 해석하고 판정하는지 묻는 질문.
  가구원으로 보는 범위, 소득·재산을 계산할 때 무엇이 들어가거나 빠지는지, 어떤 처지가 자격 요건에 해당하는지,
  지급·참여 조건의 세부 규정을 묻는 경우다. 사용자가 자기 처지를 함께 말해도 묻는 것이 기준의 해석이면 guide다.
- search: 자기 정보(나이·소득·지역·가구 등)를 덧붙여 다시 찾아 달라는 말, 안내받은 제도와 다른 도움이나
  다른 제도를 찾는 질문, 신청 방법·기간·금액처럼 제도 안내 내용을 다시 묻는 질문.
- 어느 쪽인지 분명하지 않으면 search다."""


def is_guide_question(question: str, recs: list[Recommendation]) -> bool:
    """판정 기준 문서로 답할 이어진 질문인가. 안내받은 제도가 없거나 판단에 실패하면 False."""
    names = [r.service_name for r in recs if r.service_name]
    if not (question or "").strip() or not names:
        return False
    user = "안내받은 제도: " + ", ".join(names) + "\n이어서 한 질문: " + question.strip()
    try:
        kind = condition_extractor._chat_json(PROMPT, user).get("kind")
    except Exception as e:  # 네트워크·키·JSON 오류: 지금처럼 다시 추천한다
        log.warning("판정 기준 질문 판단 실패, 다시 추천으로 진행: %s", e)
        return False
    return kind == "guide"
