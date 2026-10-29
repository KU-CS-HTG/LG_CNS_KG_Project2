"""이어진 질문을 판정 기준 문서로 답할지 LLM이 판단한다 (X1 답변 연결).

추천 카드가 나온 뒤의 이어진 질문 중 '같은 가구인가', '이 소득도 들어가나'처럼 안내받은 제도의 자격 기준을
어떻게 해석하는지 묻는 질문은 보조금24의 짧은 선정기준으로는 답할 수 없고 판정 기준 문서(고시·사업안내)에 있다.
그런 질문은 버튼을 누르지 않아도 guide_evidence.answer_guide_question으로 보낸다. 나머지(정보 덧붙이기·다른 도움
찾기)는 지금처럼 질문을 합쳐 다시 추천한다. 판단에 실패하면 다시 추천하는 쪽으로 둔다 (지금 동작과 같다).
안내받은 제도의 운영 세부(세부 신청 장소·절차, 지급 기간, 중간에 취업·이사했을 때 처리)도 guide로 본다: 처음에는
'신청 방법·기간'을 search로 두었는데, '청년 분리지급은 어디에 신청해요?'·'구직촉진수당은 얼마 동안 받아요?'가 다시
추천으로 가서 질문과 상관없는 제도 카드만 나왔다. '반려동물 병원비도 지원받을 수 있나요?'(생계급여 안내 뒤)처럼 '~도
지원받을 수 있나요'는 안내받은 제도의 지원 범위를 묻는 것으로 본다: 다른 도움으로 보고 다시 추천했더니 앞 질문과
합쳐져 생계 제도만 다시 나왔다. 다른 제도는 '다른 제도'·'추천'·'~하는 제도가 있나요'처럼 분명히 말할 때만 찾는다.
guide로 보낸 질문은 판정 문서에 관련 조항이 없어도 다시 추천하지
않고 '찾을 수 없어요'로 답한다 (기준을 물었는데 제도 카드가 나오면 답이 아니다).
LLM 호출은 condition_extractor._chat_json (다른 LLM 호출과 같은 모델·설정).
프롬프트에 예시 문장을 넣지 않는다 (condition_extractor 머리말: 예시를 그대로 따라 함).
"""
from __future__ import annotations

import logging

from src.common.schema import Recommendation
from src.generate import condition_extractor

log = logging.getLogger(__name__)

PROMPT = """너는 정부 지원제도 상담에서, 제도를 안내받은 사용자가 이어서 한 질문의 종류를 고르는 도구다.
JSON 객체 하나만 출력한다: {"kind": "guide" 또는 "search"}

- guide: 안내받은 제도가 어떻게 판정되고 운영되는지 묻는 질문.
  가구원으로 보는 범위, 소득·재산을 계산할 때 무엇이 들어가거나 빠지는지, 어떤 처지가 자격 요건에 해당하는지,
  지급·참여 조건의 세부 규정, 제도 안의 특례·세부 사업을 어디에·어떻게 신청하는지,
  지급·참여 기간(몇 개월·얼마 동안 받거나 참여하는지, 금액이 아니라 기간을 묻는 것),
  중간에 취업·이사·소득 변화가 생기면 어떻게 되는지를 묻는 경우다.
  '~도 지원받을 수 있나요?', '~비도 나오나요?'처럼 제도라는 말 없이 어떤 비용·항목도 지원되는지 묻는 질문은
  안내받은 제도의 지원 범위를 묻는 것이므로 guide다.
  사용자가 자기 처지를 함께 말해도 묻는 것이 제도의 기준·운영이면 guide다.
- search: 자기 정보(나이·소득·지역·가구 등)만 덧붙여 다시 찾아 달라는 말, '다른 제도', '비슷한 제도', '추천',
  '~을 지원하는 제도가 있나요'처럼 안내받은 제도 말고 다른 제도를 찾는다고 분명히 말한 질문,
  안내받은 제도 자체의 일반 신청 방법·신청 기한·받는 금액(얼마를 받는지)을 다시 묻는 질문
  (이것은 안내 카드에 이미 나와 있다).
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
