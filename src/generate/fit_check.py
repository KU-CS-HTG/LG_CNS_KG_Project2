"""답변 직전 '실제로 이 사람에게 맞는가' 점검 (LLM 한 번, 후보 전체를 한 번에).

검색·판정은 키워드와 분야가 맞으면 통과시킨다. 그래서 형식상 기준에는 맞아도 실제로는 부적절한 제도가 상위에 올랐다
('K-Digital Training을 듣고 있는데 이것 말고 받을 수 있는 지원' 질문에 '글로벌 광고인재 육성'이 1번: 또 다른 훈련이고 광고 직군 전용).
이 점검은 질문 전체(이미 이용 중인 것, '이것 말고' 같은 요청, 상황)를 읽고 그런 제도를 안내에서 뺀다.

LLM이 근거 없이 과하게 빼는 것을 막으려고, 뺄 때는 **자료에서 글자 그대로 복사한 근거 구절**을 반드시 받고 코드가 대조한다
(구직자 취업지원 서비스 제공을 'K-Digital Training이 이미 취업 지원을 해 준다'며 빼던 문제: 그런 말은 이용 중인 제도 설명에 없었다).
- duplicate(이미 가진 것과 같은 혜택·전제 조건): 근거 구절이 [이용 중인 제도 설명]에 있어야 한다.
- niche(특정 직무·업종·대상 전용)·mismatch(이름만 겹치고 실제 도움이 다름): 근거 구절이 그 후보의 [원문]에 있어야 한다.
- 근거를 못 대면 그 판정은 버리고 제도를 둔다. 모두 부적절하다고 하거나 호출이 실패해도 그대로 둔다. 제도 이름을 콕 집어 물은 질문은 점검하지 않는다."""
from __future__ import annotations

import json
import logging
import re

from src.common.schema import Chunk, Recommendation
from src.generate import benefit_tiers, condition_extractor, exclusion, trace

log = logging.getLogger(__name__)

MIN_REASON = 8
MIN_QUOTE = 4
TEXT_CHARS = 500
POOR = "poor"
CATEGORIES = ("duplicate", "niche", "mismatch")

PROMPT = """너는 정부 지원제도 안내 답변의 마지막 점검자다. 사용자 질문과 후보 제도들을 보고, 각 후보를 이 사용자에게 안내하는 것이
실제로 적절한지 판정한다. 키워드나 분야가 맞아도 실제로는 맞지 않는 제도를 걸러 내는 것이 목적이다.
JSON 객체 하나만 출력한다:
{"results": [{"id": 후보 id, "fit": "ok" 또는 "poor", "category": "duplicate" | "niche" | "mismatch" (poor일 때만),
              "quote": 근거 구절 (poor일 때만), "reason": 한 문장}]}

poor(안내하면 안 됨)는 아래 세 가지뿐이다. poor이면 category와, 자료에서 글자 그대로 복사한 quote가 반드시 있어야 한다.
- duplicate: 사용자가 이미 이용·수강·수급 중이라고 한 제도와 같은 혜택을 또 주거나(같은 훈련 과정·훈련비·수당), 그 제도의 전제 조건·
  이미 가진 자격(예: 이미 발급받은 카드)을 다시 안내하는 경우, 또는 '이것 말고'의 '이것'에 해당하는 경우.
  quote는 [이용 중인 제도 설명]에서 복사한다 (그 제도가 같은 혜택·전제 조건을 이미 준다는 구절). 설명에 그런 구절이 없으면 duplicate가 아니다.
- niche: 특정 직무·업종·전공·소속(예: 광고, 소상공인, 창업, 해외취업, 특정 학교)에만 해당해, 질문의 일반적인 상황(예: 취업준비생)에
  흔히 적용되지 않는 경우. quote는 그 후보의 [원문]에서 복사한다 (대상이 좁다는 구절).
- mismatch: 이름이나 낱말만 겹치고 실제 지원 내용이 사용자가 원하는 도움과 다른 경우. quote는 그 후보의 [원문]에서 복사한다.
서로 다른 혜택을 주는 제도(예: 훈련을 듣는 사람에게 소득 지원이나 취업 상담을 주는 제도)는 함께 받을 수 있는 경우가 많다.
'같은 취업 지원 분야', '이미 취업 관련 제도를 이용 중'이라는 이유만으로 duplicate라 하지 않는다. 후보의 '종류'가
[이용 중인 제도 종류]와 다르면 duplicate가 아니다 (전제 조건을 다시 안내하는 경우는 제외).
확실하지 않으면 ok다. 후보 목록의 모든 id를 results에 한 번씩 넣는다."""


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _text(chunks: list[Chunk] | None) -> str:
    return benefit_tiers.source_text(chunks or [])[:TEXT_CHARS]


def _candidate(r: Recommendation, text: str) -> dict:
    item = {"id": r.service_id or r.service_name, "이름": r.service_name, "기관": r.agency, "지원내용": r.benefit[:200],
            "자격 조건": [n[:80] for n in r.needs_check[:6]]}
    if text:
        item["원문"] = text
    kind = exclusion.CANDIDATE_KINDS.get().get(r.service_id)
    if kind:
        item["종류"] = kind
    return item


def _valid(item: dict, candidates: dict[str, str], in_use_text: str) -> bool:
    """poor 판정이 형식(범주·이유·근거 구절)을 갖췄고 근거 구절이 맞는 자료에 글자 그대로 있는가."""
    quote, reason = _squash(str(item.get("quote") or "")), str(item.get("reason") or "").strip()
    if item.get("category") not in CATEGORIES or len(quote) < MIN_QUOTE or len(reason) < MIN_REASON:
        return False
    source = in_use_text if item["category"] == "duplicate" else candidates.get(str(item["id"]), "")
    return quote in _squash(source)


def drop_unfit(question: str, recs: list[Recommendation], asked: list[str] | None = None,
               evidence: dict[str, list[Chunk]] | None = None) -> list[Recommendation]:
    """부적절한 제도를 뺀 목록 (순서는 그대로). 점검을 할 수 없거나 모두 부적절하면 입력 그대로.
    evidence: 후보별 근거 조각 (service_id -> 조각). 후보 원문을 LLM에 보여 주고 근거 구절을 대조하는 데 쓴다."""
    from src.generate import generator          # 순환 import를 피하려고 늦게 불러온다
    if len(recs) < 2 or generator._named_programs(asked):
        return recs
    ids = [r.service_id or r.service_name for r in recs]
    texts = {rid: _text((evidence or {}).get(r.service_id)) for r, rid in zip(recs, ids)}
    in_use = exclusion.IN_USE_CONTEXT.get()
    in_use_text = " ".join(f"{c['이름']} {c['설명']}" for c in in_use)
    try:
        payload = {"질문": question, "후보": [_candidate(r, texts[rid]) for r, rid in zip(recs, ids)]}
        if in_use:
            payload["이용 중인 제도 설명"] = in_use
        if exclusion.IN_USE_KINDS.get():
            payload["이용 중인 제도 종류"] = exclusion.IN_USE_KINDS.get()
        out = condition_extractor._chat_json(PROMPT, json.dumps(payload, ensure_ascii=False))
    except Exception as e:      # noqa: BLE001 — 점검이 안 되면 거르지 않는다 (맞는 제도를 잘못 빼는 쪽이 더 나쁘다)
        log.warning("실제 적합성 점검 실패, 거르지 않고 진행: %s", e)
        return recs
    verdicts: dict[str, str] = {}
    rejected: dict[str, str] = {}
    for item in out.get("results") or []:
        if not (isinstance(item, dict) and str(item.get("id")) in ids and item.get("fit") == POOR):
            continue
        rid = str(item["id"])
        if _valid(item, texts, in_use_text):
            verdicts[rid] = f"[{item['category']}] {str(item['reason']).strip()} (근거: '{str(item['quote']).strip()}')"
        else:
            rejected[rid] = f"근거 구절을 자료에서 확인하지 못해 판정을 버림: {str(item.get('reason') or '').strip()}"
    if trace.enabled():
        trace.title("실제 적합성 점검 (키워드·분야는 맞아도 이 사람에게 맞는가)")
        for r, rid in zip(recs, ids):
            trace.decision(rid, r.service_name, "제외" if rid in verdicts else "유지", verdicts.get(rid) or rejected.get(rid) or "문제 없음")
    if len(verdicts) >= len(recs):
        trace.out("  모든 후보가 부적절하다고 나와 점검 결과를 쓰지 않습니다")
        return recs
    return [r for r, rid in zip(recs, ids) if rid not in verdicts]
