"""답변 직전 '실제로 이 사람에게 맞는가' 점검 (LLM 한 번, 후보 전체를 한 번에).

검색·판정은 키워드와 분야가 맞으면 통과시킨다. 그래서 형식상 기준에는 맞아도 실제로는 부적절한 제도가 상위에 올랐다
('K-Digital Training을 듣고 있는데 이것 말고 받을 수 있는 지원' 질문에 '글로벌 광고인재 육성'이 1번: 또 다른 훈련이고 광고 직군 전용).
이 점검은 질문 전체(이미 이용 중인 것, '이것 말고' 같은 요청, 상황)를 읽고 그런 제도를 안내에서 뺀다.
- 확실하지 않으면 두고(ok), 뺄 때는 질문의 어떤 점과 맞지 않는지 이유를 반드시 받는다 (터미널 추적에 나온다).
- 모두 부적절하다고 하면 점검 자체를 믿지 않고 그대로 둔다. 호출이 실패하면 그대로 둔다.
- 제도 이름을 콕 집어 물은 질문은 점검하지 않는다."""
from __future__ import annotations

import json
import logging

from src.common.schema import Recommendation
from src.generate import condition_extractor, trace

log = logging.getLogger(__name__)

MIN_REASON = 8
POOR = "poor"

PROMPT = """너는 정부 지원제도 안내 답변의 마지막 점검자다. 사용자 질문과 후보 제도들을 보고, 각 후보를 이 사용자에게 안내하는 것이
실제로 적절한지 판정한다. 키워드나 분야가 맞아도 실제로는 맞지 않는 제도를 걸러 내는 것이 목적이다.
JSON 객체 하나만 출력한다: {"results": [{"id": 후보 id, "fit": "ok" 또는 "poor", "reason": 한 문장}]}

poor(안내하면 안 됨)로 보는 경우:
- 사용자가 이미 이용·수강·수급 중이라고 말한 것과 같은 종류라 중복되는 경우, 또는 '이것 말고', '그 외에'처럼 이미 받는 것을 빼고
  알려 달라는 요청에 맞지 않는 경우.
- 특정 직무·업종·전공·소속(예: 광고, 소상공인, 창업, 해외취업, 특정 학교)에만 해당해, 질문의 일반적인 상황(예: 취업준비생)에
  흔히 적용되지 않는 경우.
- 지원 내용이 사용자가 실제로 원하는 도움과 다른 경우 (이름이나 낱말만 겹침).
확실하지 않으면 ok다. poor일 때 reason에는 질문의 어떤 구절과 맞지 않는지 적는다.
후보 목록의 모든 id를 results에 한 번씩 넣는다."""


def _candidate(r: Recommendation) -> dict:
    return {"id": r.service_id or r.service_name, "이름": r.service_name, "기관": r.agency, "지원내용": r.benefit[:200],
            "자격 조건": [n[:80] for n in r.needs_check[:6]]}


def drop_unfit(question: str, recs: list[Recommendation], asked: list[str] | None = None) -> list[Recommendation]:
    """부적절한 제도를 뺀 목록 (순서는 그대로). 점검을 할 수 없거나 모두 부적절하면 입력 그대로."""
    from src.generate import generator          # 순환 import를 피하려고 늦게 불러온다
    if len(recs) < 2 or generator._named_programs(asked):
        return recs
    ids = [r.service_id or r.service_name for r in recs]
    try:
        out = condition_extractor._chat_json(PROMPT, json.dumps({"질문": question, "후보": [_candidate(r) for r in recs]}, ensure_ascii=False))
    except Exception as e:      # noqa: BLE001 — 점검이 안 되면 거르지 않는다 (맞는 제도를 잘못 빼는 쪽이 더 나쁘다)
        log.warning("실제 적합성 점검 실패, 거르지 않고 진행: %s", e)
        return recs
    verdicts: dict[str, str] = {}
    for item in out.get("results") or []:
        if (isinstance(item, dict) and str(item.get("id")) in ids and item.get("fit") == POOR
                and isinstance(item.get("reason"), str) and len(item["reason"].strip()) >= MIN_REASON):
            verdicts[str(item["id"])] = item["reason"].strip()
    if trace.enabled():
        trace.title("실제 적합성 점검 (키워드·분야는 맞아도 이 사람에게 맞는가)")
        for r, rid in zip(recs, ids):
            trace.decision(rid, r.service_name, "제외" if rid in verdicts else "유지", verdicts.get(rid, "문제 없음"))
    if len(verdicts) >= len(recs):
        trace.out("  모든 후보가 부적절하다고 나와 점검 결과를 쓰지 않습니다")
        return recs
    return [r for r, rid in zip(recs, ids) if rid not in verdicts]
