"""⑥ 생성 - 검색된 근거 안에서만 답변 (gpt-4o-mini + 근거 한정 프롬프트 + 인용).

규칙(CLAUDE.md '답변 생성 규칙'): 근거 없으면 모른다고 답하고 기관 안내, 자격 불명확하면 '확인 필요',
모든 답변에 서비스명·소관기관·근거 문구·상세 링크와 DISCLAIMER, 사용자 문구는 한국어.

역할 분담: LLM은 문장(해당 가능 이유·지원내용·신청방법·확인 필요 조건)과 해당 여부 판단만 쓴다.
서비스명·소관기관·근거 인용·상세 링크·신청기한은 조각에서 코드가 채운다 (지어낼 수 없게. Citation Accuracy).
"""
from __future__ import annotations

import logging
import re

from src.common import config
from src.common.schema import SECTIONS, Answer, Chunk, Recommendation
from src.generate import citation, condition_extractor
from src.generate import evidence as evidence_mod
from src.generate.citation import DISCLAIMER, NO_EVIDENCE_MESSAGE, SCOPE_NOTICE
from src.retrieve import hybrid

# 테스트 규칙: B가 끝나기 전에도 시험할 수 있도록(tests/conftest.py의 fake_search, fake_conditions)
# 다른 모듈의 함수는 `from src.retrieve import hybrid; hybrid.search(...)`,
# `condition_extractor.extract_conditions(...)`처럼 모듈 속성으로 호출한다. (`from ... import search` 금지)

log = logging.getLogger(__name__)

NOT_IN_EVIDENCE = "자료에 없음 (상세 링크에서 확인)"
NO_MATCH_MESSAGE = "질문하신 조건에 맞는 제도를 수집된 자료에서 찾지 못했습니다."
GENERIC_REASON = "질문하신 내용과 관련된 제도로 찾았어요. 자격은 아래 조건을 확인해 주세요."
MAX_SAID_CHARS = 20   # 이유에 쓰는 질문 구절 최대 길이(공백 제외). 질문 문장을 통째로 옮기는 것을 막는다
_AGE_RE = re.compile(r"\d+\s*(세|살)")

# 해당 여부. '해당 없음'(기획서 9.1: 연령·거주지가 맞지 않는 제도는 제외)과 '관련 없음'은 후보에서 뺀다.
# '관련 없음'이 필요한 이유: 검색은 관련이 없어도 Top-K를 채워 돌려주므로, 질문이 묻는 것이 근거에 없으면
# 모든 후보가 빠지고 근거 없음 응답이 나가야 한다 (답변 평가 q02·q04에서 발견).
LIKELY, UNCLEAR, NOT_ELIGIBLE, NOT_RELEVANT = "가능", "확인 필요", "해당 없음", "관련 없음"

SYSTEM_PROMPT = f"""너는 정부 지원제도 안내 도우미다. 사용자 질문과 [근거]로 주어진 제도 자료만 보고 JSON 객체 하나를 출력한다.

규칙:
- [근거]에 적힌 내용만 쓴다. 금액·연령·기간·기관·절차를 지어내거나 일반 지식으로 보충하지 않는다.
- [근거]에 해당 항목이 없으면 그 값은 "{NOT_IN_EVIDENCE}"로 쓴다.
- 먼저 각 제도가 질문과 관련 있는지 본다. 질문이 묻는 도움·제도와 관련이 없거나, 질문이 특정 제도·정보를 묻는데
  그 제도·정보가 [근거]에 없으면 applicable은 "{NOT_RELEVANT}"이다. 검색 결과라고 해서 관련 있다고 보지 않는다.
  질문이 아직 시행되지 않았거나 발표되지 않은 제도처럼 [근거]가 다룰 수 없는 것을 물으면, 비슷한 기존 제도도
  "{NOT_RELEVANT}"이다. 비슷한 제도로 대신 답하는 것은 추측이다.
- 관련 있는 제도는 사용자 조건과 제도의 자격을 비교해 applicable을 정한다:
  "{LIKELY}" = 근거에 적힌 자격 조건을 사용자가 밝힌 정보로 모두 충족함,
  "{UNCLEAR}" = 근거가 부족하거나, 사용자가 밝히지 않은 자격 조건(소득·가구·재산 등)이 하나라도 있음,
  "{NOT_ELIGIBLE}" = 근거에 적힌 연령 범위나 지역이 사용자가 질문에서 직접 밝힌 나이·거주지와 명백히 다를 때만.
  이때 mismatch에 그 근거를 적는다: said는 질문 원문에서 글자 그대로 복사한 나이·거주지 구절, condition은 근거 구절.
  사용자가 나이·거주지를 밝히지 않았거나, 지역·연령 정보가 '정보 없음'이거나, 상황이 조금 덜 맞는 정도면
  "{NOT_ELIGIBLE}"이 아니라 "{UNCLEAR}"다.
- 사용자가 밝히지 않은 사실(소득 수준, 가구 구성, 주택 소유 등)을 사용자가 충족한다고 쓰지 않는다.
- 사용자가 소득·재산 등을 밝혔더라도, [근거]에 그 기준이 금액이나 비율로 적혀 있지 않으면(기준 이하, 중위소득 등)
  충족 여부를 판단하지 않는다. 그 조건은 needs_check에 남기고 applicable은 "{LIKELY}"가 될 수 없다.
- matches에는 사용자 질문의 구절과 근거의 대상 조건이 서로 맞는 쌍만 넣는다. applicable이 "{UNCLEAR}"인 제도도
  맞는 점이 하나라도 있으면 matches를 채운다 (확인할 조건이 남아 있어도 맞는 점은 맞는 점이다).
  said는 사용자 질문 원문에서 글자 그대로 복사한 짧은 구절이어야 한다 (바꿔 쓰거나 근거의 표현을 넣지 않는다).
  said와 condition은 같은 종류의 조건끼리만 짝짓는다: 나이는 나이, 거주지는 거주지, 신분·상황(학생·구직·혼인 등)은
  신분·상황끼리. 한쪽에서 다른 종류를 추론해 짝짓지 않는다.
  condition은 근거 원문에서 글자 그대로 복사한 짧은 구절이다. 맞는 쌍이 없으면 빈 배열이다.
- 소득·재산·가구처럼 질문에 없는 조건은 matches에 넣지 말고 needs_check로 보낸다.
- needs_check에는 사용자가 밝히지 않았거나 근거로 판단할 수 없는 자격 조건을 짧은 명사구로 모두 적는다.
- 모든 문장은 한국어 존댓말로 짧게 쓴다.

출력 형식:
{{"situation": 사용자 상황을 쉼표로 이은 짧은 구,
  "services": [{{"service_id": [근거]의 id, "applicable": 해당 여부,
                "matches": [{{"said": 질문 원문 구절, "condition": 근거 원문 구절}}],
                "mismatch": {{"said": 질문 원문 구절, "condition": 근거 원문 구절}} ("{NOT_ELIGIBLE}"일 때만),
                "benefit": 지원내용 요약, "how_to_apply": 신청방법 요약, "needs_check": [문자열, ...]}}],
  "follow_up": 더 정확히 안내하려고 사용자에게 되물을 질문 1문장 (필요 없으면 빈 문자열)}}
[근거]의 모든 제도를 services에 한 번씩 넣는다."""


def generate_answer(question: str, evidence: dict[str, list[Chunk]]) -> Answer:
    if not evidence:
        return Answer(text=f"{NO_EVIDENCE_MESSAGE}\n{SCOPE_NOTICE}", no_evidence=True)

    try:
        out = condition_extractor._chat_json(SYSTEM_PROMPT, _build_prompt(question, evidence))
    except Exception as e:  # 네트워크·키·JSON 오류
        log.warning("답변 생성 실패, 원문으로 대체: %s", e)
        out = {}
    # LLM이 판정을 돌려줬으면 판정에 없는 제도는 관련 없다고 보고 뺀다.
    # 판정 자체가 없을 때(호출 실패·형식 오류)만 모든 제도를 근거 원문으로 안내한다.
    judged = isinstance(out.get("services"), list)

    by_id = {s.get("service_id"): s for s in out.get("services") or [] if isinstance(s, dict)}
    recs: list[Recommendation] = []
    for sid, chunks in evidence.items():
        gen = by_id.get(sid)
        if gen is None:
            if judged:
                continue
            gen = {}
        applicable = gen.get("applicable")
        if applicable == NOT_ELIGIBLE and not _grounded_mismatch(question, gen.get("mismatch")):
            applicable = UNCLEAR          # 질문에 나이·거주지 근거가 없으면 제외하지 않고 확인 필요로 남긴다
        if applicable in (NOT_ELIGIBLE, NOT_RELEVANT):
            continue
        recs.append(_to_recommendation(question, chunks, gen))

    if not recs:
        return Answer(text=f"{NO_MATCH_MESSAGE}\n{NO_EVIDENCE_MESSAGE}\n{SCOPE_NOTICE}", no_evidence=True)
    text = _render(recs, _str(out.get("situation")), _str(out.get("follow_up")))
    return Answer(text=text, recommendations=recs)


def _build_prompt(question: str, evidence: dict[str, list[Chunk]]) -> str:
    blocks = []
    for sid, chunks in evidence.items():
        c0, meta, sections = chunks[0], _merged_meta(chunks), _sections(chunks)
        lines = [f"[근거 id={sid}] {c0.service_name}",
                 f"- 소관기관: {meta.get('agency') or '정보 없음'}",
                 f"- 지역: {meta.get('region') or '정보 없음'}",
                 f"- 연령: {_age_text(meta)}",
                 f"- 신청기한: {meta.get('deadline') or '상시 또는 정보 없음'}"]
        for s in SECTIONS:
            lines.append(f"- {s}: {sections.get(s, '(자료 없음)')}")
        blocks.append("\n".join(lines))
    return f"[사용자 질문]\n{question}\n\n" + "\n\n".join(blocks)


def _to_recommendation(question: str, chunks: list[Chunk], gen: dict) -> Recommendation:
    """LLM 출력이 없거나 빠진 항목은 조각 원문으로 채운다."""
    meta = _merged_meta(chunks)
    raw = _sections(chunks)
    said, conds = _grounded_matches(question, chunks, gen.get("matches"))
    if said:
        reason = f"말씀하신 {', '.join(f'‘{s}’' for s in said)} 조건이 이 제도의 대상" \
                 + (f"({', '.join(f'‘{c}’' for c in conds)})" if conds else "") + "에 해당될 수 있어요."
    else:
        reason = GENERIC_REASON

    needs = _str_list(gen.get("needs_check"))
    applicable = gen.get("applicable") if said else UNCLEAR   # 질문에서 확인된 근거가 없으면 '가능'이라 하지 않는다
    if applicable != LIKELY and not needs:
        needs.append("자격 요건 충족 여부")
    for n in evidence_mod._missing_conditions(chunks) + _vague_criteria(raw):
        if n not in needs and not any(n.split()[0] in x for x in needs):
            needs.append(n)

    how = _str(gen.get("how_to_apply")) or raw.get("신청방법") or NOT_IN_EVIDENCE
    deadline = meta.get("deadline")
    how += f" / 신청기한: {deadline}까지" if deadline else " / 신청기한: 상시 또는 정보 없음"

    return Recommendation(
        service_name=chunks[0].service_name,
        agency=meta.get("agency") or citation.UNKNOWN_AGENCY,
        reason=reason,
        benefit=_str(gen.get("benefit")) or raw.get("지원내용") or NOT_IN_EVIDENCE,
        how_to_apply=how,
        needs_check=needs,
        citations=citation.build_citations(chunks),
    )


def _render(recs: list[Recommendation], situation: str, follow_up: str) -> str:
    head = f"질문하신 상황({situation})에 해당할 수 있는 지원제도입니다." if situation \
        else "질문하신 상황에 해당할 수 있는 지원제도입니다."
    parts = [head]
    for i, r in enumerate(recs, 1):
        lines = [f"{i}. {r.service_name} — {r.agency}",
                 f"   - 해당 가능 이유: {r.reason}",
                 f"   - 지원내용: {r.benefit}",
                 f"   - 신청방법·기한: {r.how_to_apply}"]
        if r.needs_check:
            lines.append(f"   - 확인 필요: {', '.join(r.needs_check)}")
        for cit in r.citations:
            lines.append(f'   - 근거: "{cit.quote}"')
        if r.citations and r.citations[0].detail_url:
            lines.append(f"   - 상세 링크: {r.citations[0].detail_url}")
        parts.append("\n".join(lines))
    if follow_up:
        parts.append(f"더 정확히 안내해 드리려면: {follow_up}")
    parts.append(f"※ {DISCLAIMER}")
    return "\n\n".join(parts)


def _grounded_matches(question: str, chunks: list[Chunk], matches) -> tuple[list[str], list[str]]:
    """LLM이 준 (질문 구절, 근거 구절) 중 질문 원문에 실제로 있는 구절만 남긴다 (나이 검증과 같은 원리).
    LLM이 제도의 대상 조건을 사용자가 말한 것처럼 옮겨 적는 일을 막는다."""
    squash = lambda s: "".join(s.split())  # noqa: E731
    q, body = squash(question), squash(" ".join(citation._body_text(c) for c in chunks))
    said: list[str] = []
    conds: list[str] = []
    for m in matches if isinstance(matches, list) else []:
        if not isinstance(m, dict):
            continue
        s, c = _str(m.get("said")).strip("‘’'\""), _str(m.get("condition")).strip("‘’'\"")
        if _AGE_RE.search(c) and not re.search(r"\d", s):
            continue                      # 나이 조건은 나이로만 맞춘다 ('대학생' -> '만 19~34세' 같은 추론 차단)
        if s and len(squash(s)) <= MAX_SAID_CHARS and squash(s) in q and s not in said:
            said.append(s)
            if c and squash(c) in body and c not in conds:
                conds.append(c)
    return said, conds


def _vague_criteria(sections: dict[str, str]) -> list[str]:
    """근거에 소득·재산 조건이 있는데 금액·비율 숫자가 없으면 사용자가 소득을 밝혀도 판단할 수 없다 ('기준 이하' 등).
    LLM이 충족으로 단정하는 일을 코드로 막는다 (프롬프트 규칙만으로는 실행마다 결과가 달랐음)."""
    text = " ".join(sections.get(s, "") for s in ("지원대상", "선정기준"))
    out = []
    for word in ("소득", "재산"):
        for m in re.finditer(word, text):
            if not re.search(r"\d", text[m.start():m.start() + 25]):
                out.append(f"{word} 기준 (자료에 기준 금액 없음)")
                break
    return out


def _grounded_mismatch(question: str, mismatch) -> bool:
    """'해당 없음' 판정의 근거(사용자가 밝힌 나이·거주지 구절)가 질문 원문에 실제로 있는가."""
    said = _str(mismatch.get("said")).strip("‘’'\"") if isinstance(mismatch, dict) else ""
    return bool(said) and "".join(said.split()) in "".join(question.split())


def _sections(chunks: list[Chunk]) -> dict[str, str]:
    """섹션 -> 본문 원문 (긴 항목이 여러 조각으로 나뉘었으면 순서대로 이어 붙인다)."""
    out: dict[str, list[str]] = {}
    for c in chunks:
        out.setdefault(c.section, []).append(citation._body_text(c))
    return {s: " ".join(texts) for s, texts in out.items()}


def _merged_meta(chunks: list[Chunk]) -> dict:
    meta: dict = {}
    for c in chunks:
        for k, v in c.metadata.items():
            if v not in (None, "", []):
                meta.setdefault(k, v)
    return meta


def _age_text(meta: dict) -> str:
    lo, hi = meta.get("age_min"), meta.get("age_max")
    if lo is None and hi is None:
        return "정보 없음"
    return f"만 {lo if lo is not None else ''}~{hi if hi is not None else ''}세"


def _str(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _str_list(v) -> list[str]:
    if isinstance(v, str):
        v = [v]
    return [s.strip() for s in v if isinstance(s, str) and s.strip()] if isinstance(v, list) else []


def answer_question(question: str) -> Answer:
    """온라인 전체 흐름: 조건 추출 -> 검색(필터 완화 재시도) -> 근거 선별 -> 생성. app은 이 함수만 호출한다.

    필터 완화(filters.relax) 재검색은 B의 hybrid.search 안에서 한다 (중복 구현하지 않음).
    """
    conditions = condition_extractor.extract_conditions(question)
    filters = condition_extractor.to_filters(conditions)
    chunks = hybrid.search(question, filters, k=config.DEFAULT_TOP_K)
    selected = evidence_mod.select_evidence(chunks)
    return generate_answer(question, selected)
