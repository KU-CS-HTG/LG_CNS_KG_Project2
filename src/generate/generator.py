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
GENERIC_REASON = "질문하신 내용과 관련된 제도로 찾았어요. 자격은 아래 조건을 확인해 주세요."
RELAXED_MARK = "조건을 넓혀 찾았어요"   # 필터 완화 안내 문구 (UI가 이 문구가 든 줄을 찾아 따로 보여 준다)
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
- 각 제도의 meets_need: [사용자가 원하는 도움]이 주어지면, 그 제도의 지원내용이 그중 하나라도 직접 도와주면 true,
  아니면 false다. 지역·나이·신분이 맞는다는 것은 meets_need의 근거가 아니다. [사용자가 원하는 도움]이 없으면 true다.- 관련 있는 제도는 사용자 조건과 제도의 자격을 비교해 applicable을 정한다:
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
- needs_check에는 사용자가 밝히지 않았거나 근거로 판단할 수 없는 자격 조건을 모두 적는다.
  item은 조건 이름(소득·재산·거주지·연령·가구 등 짧은 명사구), criterion은 그 기준이 적힌 [근거] 원문 구절을
  글자 그대로 복사한다(금액·비율·지역·나이가 적힌 부분). [근거]에 기준이 적혀 있지 않으면 criterion은 빈 문자열이다.
- 모든 문장은 한국어 존댓말로 짧게 쓴다.

출력 형식:
{{"situation": 사용자 상황을 쉼표로 이은 짧은 구,
  "services": [{{"service_id": [근거]의 id, "applicable": 해당 여부, "meets_need": true 또는 false,                "matches": [{{"said": 질문 원문 구절, "condition": 근거 원문 구절}}],
                "mismatch": {{"said": 질문 원문 구절, "condition": 근거 원문 구절}} ("{NOT_ELIGIBLE}"일 때만),
                "benefit": 지원내용 요약, "how_to_apply": 신청방법 요약,
                "needs_check": [{{"item": 조건 이름, "criterion": 근거 원문 구절 또는 ""}}]}}],
  "follow_up": 더 정확히 안내하려고 사용자에게 되물을 질문 1문장 (필요 없으면 빈 문자열)}}
[근거]의 모든 제도를 services에 한 번씩 넣는다."""


def generate_answer(question: str, evidence: dict[str, list[Chunk]]) -> Answer:
    return _generate(question, evidence, [])


def _needs_from(conditions: dict) -> list[str]:
    """사용자가 원하는 도움 = 질문에서 직접 말한 도움(help) + 처지(situation). 직접 말한 도움이 없으면
    (받을 수 있는 지원을 넓게 묻는 질문) 비워서 거르지 않는다.

    - 답변 생성 프롬프트 안에서 LLM이 needs를 직접 뽑게 했더니 흔들렸다 (관람비 질문에 빈 값, 넓은 질문에 값).
    - field만으로는 '직접 말한 도움'(등록금 -> 교육)과 '처지로 짐작한 분야'(취업준비생 -> 일자리)를 구분할 수 없다.
    그래서 조건 추출에서 질문 원문 구절로 검증된 help를 쓴다."""
    help_ = conditions.get("help") or []
    if not help_:
        return []
    situation = [s for s in conditions.get("situation") or [] if s in condition_extractor.SITUATION_DEFS]
    return list(help_) + [f"{s}({condition_extractor.SITUATION_DEFS[s]})" for s in situation]


def _generate(question: str, evidence: dict[str, list[Chunk]], needs: list[str],
              asked: list[str] | None = None) -> Answer:
    """asked = 질문에서 검증된 '찾는 것' 구절(conditions['help']). 나이·지역 짝이 없을 때 이유 문장에 쓴다."""
    if not evidence:
        return Answer(text=f"{NO_EVIDENCE_MESSAGE}\n{SCOPE_NOTICE}", no_evidence=True)

    try:
        out = condition_extractor._chat_json(SYSTEM_PROMPT, _build_prompt(question, evidence, needs))
    except Exception as e:  # 네트워크·키·JSON 오류
        log.warning("답변 생성 실패, 원문으로 대체: %s", e)
        out = {}
    # LLM이 판정을 돌려줬으면 판정에 없는 제도는 관련 없다고 보고 뺀다.
    # 판정 자체가 없을 때(호출 실패·형식 오류)만 모든 제도를 근거 원문으로 안내한다.
    judged = isinstance(out.get("services"), list)
    # 원하는 도움을 주는 제도인지는 구조화된 값(meets_need)으로 받아 코드로 적용한다
    # (applicable의 '관련 없음' 규칙만으로는 지역·나이만 맞는 제도가 섞였음: 공연 관람비 질문에 청년월세)

    # 실제 서비스ID는 숫자로만 돼 있어('568000000347') LLM이 JSON 숫자로 돌려주기도 한다 -> 문자열로 맞춘다
    by_id = {str(s.get("service_id")).strip(): s for s in out.get("services") or [] if isinstance(s, dict)}
    recs: list[Recommendation] = []
    for sid, chunks in evidence.items():
        gen = by_id.get(sid)
        if gen is None:
            if judged:
                continue
            gen = {}
        applicable = gen.get("applicable")
        if applicable == NOT_ELIGIBLE and not _verified_mismatch(question, gen.get("mismatch")):
            applicable = UNCLEAR          # 나이·거주지가 실제로 다르다고 확인되지 않으면 제외하지 않고 확인 필요로 남긴다
        if applicable in (NOT_ELIGIBLE, NOT_RELEVANT):
            continue
        if needs and gen.get("meets_need") is False:
            continue                      # 원하는 도움을 주지 않는 제도 (지역·나이만 맞는 제도가 섞이는 것 방지)
        recs.append(_to_recommendation(question, chunks, gen, _met_asked(chunks, needs, asked)
                                       if gen.get("meets_need") is True else None))

    if not recs:
        return Answer(text=f"{NO_EVIDENCE_MESSAGE}\n{SCOPE_NOTICE}", no_evidence=True)
    text = _render(recs, _str(out.get("situation")), _str(out.get("follow_up")))
    return Answer(text=text, recommendations=recs)


_JOSA_RE = re.compile(r"(?:이나|으로|에서|에게|까지|부터|을|를|이|가|은|는|도|나|의|에|와|과|로)$")


def _met_asked(chunks: list[Chunk], needs: list[str], asked: list[str] | None) -> list[str] | None:
    """meets_need가 true인 제도의 이유 문장에 쓸 '원하는 도움' 구절 (코드 검증).

    needs에는 질문 구절(help)과 처지(구직 등)가 함께 있어 LLM의 meets_need가 처지 때문에 true일 수 있다.
    그때 질문 구절로 이유를 붙이면 틀린 이유가 됐다 ('월세가 부담되고'에 국민취업지원제도, 평가 q01·q10).
    그래서 구절의 핵심 단어('월세')가 제도 원문에 있는 구절만 쓰고, 하나도 없으면 처지('구직 상황')로 쓴다.
    (LLM에 어느 도움을 충족했는지 함께 묻는 방법은 항목 하나만 늘어도 meets_need 판정이 흔들려 쓰지 않는다.)"""
    if not asked:
        return None
    body = re.sub(r"[^0-9A-Za-z가-힣]", "", " ".join(c.text for c in chunks))     # 서비스명 포함
    def in_body(phrase: str) -> bool:
        words = (_JOSA_RE.sub("", w) for w in re.findall(r"[0-9A-Za-z가-힣]+", phrase))
        return any(len(w) >= 2 and w not in condition_extractor._GENERIC_HELP and w in body for w in words)
    phrases = [a for a in asked if in_body(a)]
    situations = [n.split("(")[0] for n in needs if n not in asked]
    return phrases or [f"{s} 상황" for s in situations] or asked


def _build_prompt(question: str, evidence: dict[str, list[Chunk]], needs: list[str] | None = None) -> str:
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
    head = f"[사용자 질문]\n{question}\n\n"
    if needs:
        head += "[사용자가 원하는 도움]\n" + "\n".join(f"- {n}" for n in needs) + "\n\n"
    return head + "\n\n".join(blocks)


def _to_recommendation(question: str, chunks: list[Chunk], gen: dict, asked: list[str] | None = None) -> Recommendation:
    """LLM 출력이 없거나 빠진 항목은 조각 원문으로 채운다.

    asked: 이 제도가 도와준다고 판정된(meets_need) '찾는 것' 구절. 나이·거주지 짝이 없을 때 일반 문구 대신 쓴다
    (실제 데이터: 나이·지역 없이 '면접 비용 지원'만 물으면 모든 카드의 이유가 같은 일반 문구였다)."""
    meta = _merged_meta(chunks)
    raw = _sections(chunks)
    llm_pairs = _grounded_matches(question, chunks, gen.get("matches"))
    said, conds = _merge_pairs(_code_matches(question, chunks), llm_pairs)
    if said:
        reason = f"말씀하신 {', '.join(f'‘{s}’' for s in said)} 조건이 이 제도의 대상" \
                 + (f"({', '.join(f'‘{c}’' for c in conds)})" if conds else "") + "에 해당될 수 있어요."
    elif asked:
        reason = f"말씀하신 {', '.join(f'‘{a}’' for a in asked)}에 도움이 될 수 있는 제도예요. 자격은 아래 조건을 확인해 주세요."
    else:
        reason = GENERIC_REASON

    needs = _needs_with_criteria(gen.get("needs_check"), chunks)
    # 질문에서 확인된 근거가 없으면 '가능'이라 하지 않는다. 코드가 만든 나이·지역 짝은 이유 문장에만 쓰고,
    # '가능'은 LLM 자신의 판단 근거(짝)가 검증될 때만 인정한다 (LLM이 사용자가 말하지 않은 '무주택'으로 '가능'이라 한 경우 차단)
    applicable = gen.get("applicable") if llm_pairs[0] else UNCLEAR
    for n in evidence_mod._missing_conditions(chunks) + _vague_criteria(raw):
        key = n.split()[0]
        same = [i for i, x in enumerate(needs) if key in x]
        if not same:
            needs.append(n)
        elif " — " in n and all(" — " not in needs[i] for i in same):
            needs[same[0]] = n            # LLM은 이름만 줬는데 코드가 원문에서 기준 구절을 찾은 경우
    if applicable != LIKELY and not needs:
        needs.append("자격 요건 충족 여부")   # 확인할 조건을 하나도 못 찾았을 때만 (다른 조건과 함께 나오면 군더더기)

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
    # 근거 구절은 대상 조건(지원대상·선정기준)에서만 받는다. 지원내용과 짝지으면 '말씀하신 '이자를 지원받고 싶어'
    # 조건이 이 제도의 대상('대출금에 대한 이자 지원')'처럼 원하는 도움이 조건으로 둔갑했다
    target = [c for c in chunks if c.section in ("지원대상", "선정기준")]
    q, body = squash(question), squash(" ".join(citation._body_text(c) for c in target))
    said: list[str] = []
    conds: list[str] = []
    for m in matches if isinstance(matches, list) else []:
        if not isinstance(m, dict):
            continue
        s, c = _str(m.get("said")).strip("‘’'\""), _str(m.get("condition")).strip("‘’'\"")
        age_only = len(squash(_AGE_RE.sub("", s)).replace("만", "")) <= 1     # '27살', '만 27세'처럼 나이뿐인 구절
        if (_AGE_RE.search(c) and not re.search(r"\d", s)) or (age_only and not _AGE_RE.search(c)):
            continue                      # 나이는 나이끼리만 짝짓는다 ('대학생'↔'만 19~34세', '27살'↔'구직자' 차단)
        # 짝의 양쪽이 모두 검증돼야 쓴다: 질문 구절은 질문 원문에, 근거 구절은 근거 원문에 있어야 한다
        # (근거에 없는 나이 조건과 짝지은 '27살'이 이유에 혼자 남던 문제)
        if not (s and c and len(squash(s)) <= MAX_SAID_CHARS and squash(s) in q and squash(c) in body):
            continue
        if s not in said:
            said.append(s)
        if c not in conds:
            conds.append(c)
    # 다른 구절에 포함되는 구절은 뺀다 ('서울에 사는 27살 취업준비생', '27살' -> 앞의 것만)
    said = [s for s in said if not any(s != o and squash(s) in squash(o) for o in said)]
    return said, conds


MAX_CRITERION_CHARS = 60   # 확인 필요 기준 구절 최대 길이(공백 제외). 선정기준 문단을 통째로 옮기는 것을 막는다
NO_CRITERION = "(기준은 원문 확인)"   # 기준 구절을 옮겨 오지 못한 확인 필요 조건에 붙이는 안내
MAX_REGION_PIECE_CHARS = 40   # 이유 문장에 넣는 거주지 근거 구절 최대 길이 (긴 전입 조건 문장이 그대로 들어가지 않게)


def _needs_with_criteria(items, chunks: list[Chunk]) -> list[str]:
    """확인 필요 조건에 근거 원문의 기준을 붙인다: '소득 — 기준 중위소득 150% 이하'.

    이름만 있으면('소득 기준') 사용자가 결국 원문을 열어 봐야 했다. 기준 구절은 근거 원문에 글자 그대로 있을 때만
    붙인다 (LLM이 금액·비율을 지어내거나 바꿔 쓰는 일 차단, 이유 문장의 matches와 같은 원리).
    기준 구절이 없으면 그 조건 이름이 원문에 나올 때만 '(기준은 원문 확인)'을 붙여 남기고, 원문에 없는 조건은 뺀다."""
    # 글자·숫자·%만 남겨 비교한다: 원문의 각주 표시('원가구*의')·괄호·따옴표를 LLM이 빼고 옮겨도 같은 구절로 본다
    # (내용은 정확한데 '*' 하나 때문에 기준이 빠졌다). 금액·비율 숫자는 그대로 검증된다.
    squash = lambda s: re.sub(r"[^0-9A-Za-z가-힣%]", "", s)  # noqa: E731
    body = squash(" ".join(citation._body_text(c) for c in chunks))
    out: list[str] = []
    if isinstance(items, (str, dict)):
        items = [items]
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            name, crit = _str(it.get("item")).strip(), _str(it.get("criterion")).strip().strip("‘’'\"")
        else:                                  # 예전 형식(문자열)도 받는다
            name, crit = _str(it).strip(), ""
        if not name:
            continue
        if crit and (len(squash(crit)) > MAX_CRITERION_CHARS or squash(crit) not in body):
            crit = ""
        if crit:
            text = f"{_criterion_label(crit, name)} — {crit}"
        elif any(len(w) >= 2 and squash(w) in body for w in name.split()):
            text = f"{name} {NO_CRITERION}"     # 원문에 그 조건이 언급은 되지만 기준 구절을 옮겨 오지 못함
        else:
            continue                           # 원문에 없는 조건 (월세자금보증에 근거 없이 '가구'가 붙던 문제)
        if text not in out:
            out.append(text)
    return out


def _code_matches(question: str, chunks: list[Chunk]) -> tuple[list[str], list[str]]:
    """나이·거주지 짝은 코드가 직접 만든다: 질문의 '26살'·'서울' ↔ 근거 지원대상·선정기준의 연령 범위·시도 구절.

    LLM이 said에 질문 문장 전체를 넣어(20자 제한에 걸려) 나이·지역이 맞는데도 이유가 일반 문구가 되는 일이 잦았다.
    나이는 범위 안일 때만, 지역은 같은 시·도가 원문에 있을 때만 짝짓는다."""
    target = " ".join(citation._body_text(c) for c in chunks if c.section in ("지원대상", "선정기준"))
    said: list[str] = []
    conds: list[str] = []
    age = _AGE_RE.search(question)
    if age:
        n = int(re.match(r"\d+", age.group()).group())
        for m in _AGE_RANGE_RE.finditer(target):
            if int(m.group(1)) <= n <= int(m.group(2)):
                said.append(age.group().strip())
                conds.append(m.group().strip())
                break
    region = condition_extractor._find_region(question)
    if region and region in _regions_in(target):
        word = next((w for w in question.split() if region in w or any(
            full in w for full, short in condition_extractor._REGION_ALIASES.items() if short == region)), region)
        names = [region, *(full for full, short in condition_extractor._REGION_ALIASES.items() if short == region)]
        for piece in re.split(r"[,，·\n○ㅇ※*()]|\s-\s", target):
            piece = piece.strip(" -•▪◦")
            if any(nm in piece for nm in names) and len("".join(piece.split())) <= MAX_REGION_PIECE_CHARS:
                said.append(word.strip(".,?!"))
                conds.append(piece)
                break
    return said, conds


def _merge_pairs(*pairs: tuple[list[str], list[str]]) -> tuple[list[str], list[str]]:
    """여러 출처의 (질문 구절들, 근거 구절들)을 합치고, 다른 구절에 들어 있는 구절은 뺀다.
    기호는 무시하고 비교한다 ('- 부산광역시에 주민등록…'과 '부산광역시에 주민등록…(배우자 포함)'이 둘 다 나오던 문제)."""
    squash = lambda s: re.sub(r"[^0-9A-Za-z가-힣%]", "", s)  # noqa: E731
    said = list(dict.fromkeys(s for p in pairs for s in p[0]))
    conds = list(dict.fromkeys(c for p in pairs for c in p[1]))
    said = [s for s in said if not any(s != o and squash(s) in squash(o) for o in said)]
    conds = [c for c in conds if not any(c != o and squash(c) in squash(o) for o in conds)]
    return said, conds


# 기준 구절 내용으로 조건 이름을 붙인다 (LLM이 '보증금 8천만원 이하, 월세 60만원 이하'를 '거주지'라고 하는 등 이름이 어긋났다).
# 위에서부터 먼저 맞는 것을 쓴다. 맞는 게 없으면 LLM이 준 이름을 그대로 쓴다.
_CRITERION_LABELS = (
    ("소득", re.compile(r"소득|학자금\s*지원\s*\d*\s*구간|차상위|수급")),
    ("재산", re.compile(r"재산|자산|자동차")),
    ("주택 조건", re.compile(r"보증금|월세|전세|임차|주택|무주택")),
    ("거주지", re.compile("|".join(("도내", "관내", "전입", "주민등록", *condition_extractor.REGIONS,
                                    *condition_extractor._REGION_ALIASES)))),
    ("연령", re.compile(r"\d+\s*세")),
)


def _criterion_label(criterion: str, fallback: str) -> str:
    for label, pattern in _CRITERION_LABELS:
        if pattern.search(criterion):
            return label
    return fallback


_CRITERION_SPLIT_RE = re.compile(r"[()（）\[\]·•○ㅇ※\n]|\s-\s|,\s")
# 금액·비율 형태의 숫자 ('6천만원', '150%', '9구간'). 나이('19~34세')를 소득 기준으로 착각하지 않게
_AMOUNT_RE = re.compile(r"\d[\d,.]*\s*(?:천|만|억|원|%|퍼센트|구간|분위)")


def _vague_criteria(sections: dict[str, str]) -> list[str]:
    """근거의 소득·재산 조건을 코드가 직접 정리한다 (LLM이 기준을 빠뜨려도 나오게).

    - 숫자(금액·비율)가 붙은 구절이 있으면 '소득 — 본인 연소득 6천만원 이하'처럼 원문 구절을 붙인다.
    - 그런 구절이 하나도 없을 때만 '기준 금액 없음'으로 둔다: 사용자가 소득을 밝혀도 판단할 수 없으므로
      LLM이 충족으로 단정하는 일을 막는다 (프롬프트 규칙만으로는 실행마다 결과가 달랐음).
    예전에는 '소득'이 나오는 곳 중 한 군데라도 숫자가 없으면('소득 기준은 은행 심사로 결정') 기준 없음으로 판정했다."""
    text = " ".join(sections.get(s, "") for s in ("지원대상", "선정기준"))
    out = []
    for word in ("소득", "재산"):
        if word not in text:
            continue
        pieces = [p.strip(" ,:;") for p in _CRITERION_SPLIT_RE.split(text)]
        hits = [p for p in pieces if word in p and _AMOUNT_RE.search(p) and len("".join(p.split())) <= MAX_CRITERION_CHARS]
        if hits:
            out.append(f"{word} — {', '.join(dict.fromkeys(hits[:2]))}")
        else:
            out.append(f"{word} 기준 (자료에 기준 금액 없음)")
    return out


_AGE_RANGE_RE = re.compile(r"(\d+)\s*세?\s*[~∼\-–]\s*(?:만\s*)?(\d+)\s*세")
_AGE_MAX_RE = re.compile(r"(\d+)\s*세\s*(이하|미만)")
_AGE_MIN_RE = re.compile(r"(\d+)\s*세\s*(이상|초과)")


def _verified_mismatch(question: str, mismatch) -> bool:
    """'해당 없음' 판정을 코드로 확인한다. 확인되지 않으면 빼지 않고 '확인 필요'로 남긴다.

    - said가 질문 원문에 있고, 나이나 거주지를 담은 구절이어야 한다
      (실제 데이터: 질문 문장 전체를 said로 써서 청년도약계좌 질문의 제도가 모두 빠짐).
    - 나이면 근거의 연령 범위를 코드가 읽어 실제로 벗어나는지 본다 ('27살' vs '18세~45세'를 해당 없음으로 판정한 적 있음).
    - 거주지면 근거 구절이 다른 시·도를 말하고 사용자의 시·도는 말하지 않아야 한다."""
    if not isinstance(mismatch, dict):
        return False
    said = _str(mismatch.get("said")).strip("‘’'\"")
    cond = _str(mismatch.get("condition"))
    if not said or "".join(said.split()) not in "".join(question.split()):
        return False

    age = _AGE_RE.search(said)
    if age:
        n = int(re.match(r"\d+", age.group()).group())
        if m := _AGE_RANGE_RE.search(cond):
            return not int(m.group(1)) <= n <= int(m.group(2))
        lo, hi = _AGE_MIN_RE.search(cond), _AGE_MAX_RE.search(cond)
        if not (lo or hi):
            return False                              # 근거 구절에서 연령 범위를 읽을 수 없음
        below = lo and (n < int(lo.group(1)) or (lo.group(2) == "초과" and n == int(lo.group(1))))
        above = hi and (n > int(hi.group(1)) or (hi.group(2) == "미만" and n == int(hi.group(1))))
        return bool(below or above)

    user = condition_extractor._find_region(said)
    if user is None:
        return False                                  # 나이도 시·도도 아닌 구절
    named = _regions_in(cond)
    return bool(named) and user not in named


def _regions_in(text: str) -> set[str]:
    return {r for r in condition_extractor.REGIONS if r in text} | {
        short for full, short in condition_extractor._REGION_ALIASES.items() if full in text}


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


def answer_question(question: str) -> Answer:
    """온라인 전체 흐름: 조건 추출 -> 검색(필터 완화 재시도) -> 근거 선별 -> 생성. app은 이 함수만 호출한다.

    필터 완화(filters.relax) 재검색은 B의 hybrid.search_with_info 안에서 한다 (중복 구현하지 않음).
    """
    return _answer_with_evidence(question)[0]


def _answer_with_evidence(question: str) -> tuple[Answer, dict[str, list[Chunk]]]:
    """answer_question의 본체. 생성에 넘긴 근거도 함께 돌려준다 (답변 평가가 같은 흐름으로 채점하도록)."""
    conditions = condition_extractor.extract_conditions(question)
    filters = condition_extractor.to_filters(conditions)
    region = conditions.get("region")
    # CLAUDE.md: 완화 여부가 필요하면 search_with_info. search와 결과는 같고 완화 정보가 더 붙는다.
    # 지역을 말했으면 후보를 넉넉히 받아 다른 지역 지자체 제도를 뺀 뒤 상위 5개를 고른다 (_drop_other_regions)
    k = config.DEFAULT_TOP_K * REGION_CANDIDATE_FACTOR if region else config.DEFAULT_TOP_K
    result = hybrid.search_with_info(question, filters, k=k)
    chunks, region_relaxed = _drop_other_regions(result.chunks, region)
    selected = evidence_mod.select_evidence(chunks)
    answer = _generate(question, selected, _needs_from(conditions), conditions.get("help"))
    used = dict(result.filters_used)
    if region_relaxed:
        used.pop("region", None)
    notice = _relaxed_notice(filters, used) if (result.relaxed or region_relaxed) else ""
    if notice and not answer.no_evidence:
        answer.text = f"{notice}\n\n{answer.text}"
    return answer, selected


REGION_CANDIDATE_FACTOR = 3   # 지역을 말했을 때 받는 검색 후보 배수 (5 -> 15개 중 다른 지역 제도를 빼고 5개)


def _drop_other_regions(chunks: list[Chunk], region: str | None) -> tuple[list[Chunk], bool]:
    """사용자가 말한 시·도와 다른 지역의 지자체 제도를 뺀다. (남은 조각, 지역 조건을 풀었는가)

    수집 데이터에 지역 메타데이터가 없어 B의 지역 필터가 아무것도 거르지 못하고, 원문에 '충주시'만 있으면
    시·도 비교도 안 돼 '대구 사는 24살' 질문에 충주시 면접비 지원이 나왔다. 소관기관이 지자체('충청북도 충주시')면
    그 시·도를 제도의 지역으로 본다. 전국 제도(중앙부처·공공기관)는 남긴다.
    다 빠지면 원래 후보를 그대로 쓰고 지역 조건을 풀었다고 알린다 (소프트 필터, CLAUDE.md)."""
    if not region:
        return chunks, False
    keep = [c for c in chunks if _agency_region(c.metadata.get("agency") or "") in (None, region)]
    return (keep, False) if keep else (chunks, True)


def _agency_region(agency: str) -> str | None:
    """지자체 소관기관의 시·도 짧은 이름 ('충청북도 충주시' -> '충북'). 전국 기관이면 None."""
    if not evidence_mod._LOCAL_AGENCY_RE.search(agency):
        return None
    return condition_extractor._find_region(agency)


# B가 결과가 없을 때 푸는 조건 (CLAUDE.md: 지역 -> 연령. 관심 분야는 필터가 아니라 가산점이라 풀지 않는다)
_RELAX_LABELS = {"region": "지역", "age": "나이"}


def _relaxed_notice(asked: dict, used: dict) -> str:
    """B가 결과가 없어 푼 조건을 사용자 문구로 알린다 (소프트 필터를 투명하게)."""
    dropped = [f"{label}({asked[key]}{'세' if key == 'age' else ''})"
               for key, label in _RELAX_LABELS.items() if key in asked and key not in used]
    if not dropped:
        return ""
    return f"말씀하신 {', '.join(dropped)} 조건에 맞는 제도를 찾지 못해 {RELAXED_MARK}. " \
           "아래 제도가 그 조건과 다를 수 있으니 확인해 주세요."
