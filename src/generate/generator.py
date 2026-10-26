"""⑥ 생성 - 검색된 근거 안에서만 답변 (gpt-4o-mini + 근거 한정 프롬프트 + 인용).

규칙(CLAUDE.md '답변 생성 규칙'): 근거 없으면 모른다고 답하고 기관 안내, 자격 불명확하면 '확인 필요',
모든 답변에 서비스명·소관기관·근거 문구·상세 링크와 DISCLAIMER, 사용자 문구는 한국어.

역할 분담: LLM은 문장(해당 가능 이유·지원내용·신청방법·확인 필요 조건)과 해당 여부 판단만 쓴다.
서비스명·소관기관·근거 인용·상세 링크·신청기한은 조각에서 코드가 채운다 (지어낼 수 없게. Citation Accuracy).
"""
from __future__ import annotations

import calendar
import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import date
from pathlib import Path

from src.common import config
from src.common.regions import normalize_region
from src.common.schema import SECTIONS, Answer, Chunk, Recommendation
from src.catalog import store as catalog_store
from src.generate import apply_link, benefit_detail, citation, condition_extractor, exclusion, period_detail, topical, trace
from src.generate import evidence as evidence_mod
from src.generate.citation import DISCLAIMER, NO_EVIDENCE_MESSAGE, SCOPE_NOTICE
from src.ingest import store as ingest_store   # A: 신청기한 원문 조회 (모듈 속성으로 호출, 테스트에서 바꿔 끼움)
from src.retrieve import filters as retrieve_filters   # B: 관심 분야 비교 (구 제도 거르기)
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
  아니면 false다. 지역·나이·신분이 맞는다는 것은 meets_need의 근거가 아니다. [사용자가 원하는 도움]이 없으면 true다.
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


CHECKLIST_PROMPT = """너는 정부 지원제도의 신청 자격을 체크리스트로 정리한다. 각 [제도]의 지원대상·선정기준만 보고,
신청자가 스스로 해당 여부를 확인할 수 있는 자격 조건을 뽑아 JSON 객체 하나를 출력한다.

규칙:
- 조건마다 item과 criterion을 적는다.
  item: 조건의 짧은 이름 (연령, 거주지, 소득, 재산, 주택, 세대주, 혼인, 자녀, 재학, 취업 상태, 성적, 국적 등).
  criterion: 그 조건이 적힌 [제도] 원문 구절을 글자 그대로 복사한 짧은 부분 (40자 이내, 바꿔 쓰지 않는다).
- 한 문장에 여러 조건이 있으면 나눈다. 예: '만 19~34세 이하로 부모님과 별도 거주하는 무주택자'
  -> 연령 '만 19~34세 이하', 거주 형태 '부모님과 별도 거주', 주택 '무주택자'.
- 지원 내용, 신청 방법, 제출 서류, 사업 기간, 지원 규모, 선발 순위, 문의처는 자격 조건이 아니다.
- 대상 유형이 여럿이면(청년 / 신혼부부) item에 유형을 붙인다. 예: '신혼부부 혼인 기간'.
- 대상 자체를 정하는 조건부터 최대 6개. 원문에 자격 조건이 없으면 빈 배열이다.

출력 형식: {"services": [{"id": [제도]의 id, "conditions": [{"item": 조건 이름, "criterion": 원문 구절}]}]}"""
MAX_CHECKLIST_ITEMS = 6


def _extract_checklists(evidence: dict[str, list[Chunk]]) -> dict[str, list[dict]]:
    """제도별 자격 체크리스트 원본 (LLM, 근거 검증 전). 실패하면 빈 dict (답변 생성의 needs_check로 대신한다).

    답변 생성 LLM이 덤으로 적는 needs_check는 소득 한두 줄만 나오고 무주택·세대주·자녀 수·재학 같은 핵심 자격이
    빠졌다 (청년월세·버팀목·다자녀 국가장학금). 자격만 뽑는 호출을 따로 두고, 답변 생성과 동시에 돌려 시간이 늘지 않게 한다.
    제도마다 따로(동시에) 묻는다: 5개를 한 번에 물으면 응답이 길어 6.5초로 답변 시간의 대부분이었고(판정 3.4초),
    판정 후보를 10개로 늘리면 더 길어진다. 실패한 제도만 빠진다."""
    blocks = {}
    for sid, chunks in evidence.items():
        sections = _sections(chunks)
        body = "\n".join(f"- {s}: {sections[s]}" for s in ("지원대상", "선정기준") if sections.get(s))
        if body:
            blocks[sid] = f"[제도 id={sid}] {chunks[0].service_name}\n{body}"
    if not blocks:
        return {}
    with ThreadPoolExecutor(max_workers=min(len(blocks), MAX_JUDGE_WORKERS)) as pool:
        outs = dict(zip(blocks, pool.map(_checklist_one, blocks.values())))
    result: dict[str, list[dict]] = {}
    for sid, out in outs.items():
        services = [s for s in (out or {}).get("services") or []
                    if isinstance(s, dict) and isinstance(s.get("conditions"), list)]
        own = [s for s in services if str(s.get("id")).strip() == sid] or services[:1]   # 하나만 물었으니 id가 달라도 그 제도
        if own:
            result[sid] = own[0]["conditions"][:MAX_CHECKLIST_ITEMS]
    return result


def _checklist_one(block: str) -> dict | None:
    """제도 하나의 자격 체크리스트 (LLM 1번). 실패하면 None (그 제도는 답변 생성의 needs_check로 대신한다)."""
    try:
        return condition_extractor._chat_json(CHECKLIST_PROMPT, block)
    except Exception as e:  # 네트워크·키·JSON 오류
        log.warning("자격 체크리스트 추출 실패, 답변 생성의 확인 필요 조건으로 대체: %s", e)
        return None


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
              asked: list[str] | None = None, region: str | None = None) -> Answer:
    """asked = 질문에서 검증된 '찾는 것' 구절(conditions['help']). 나이·지역 짝이 없을 때 이유 문장에 쓴다."""
    if not evidence:
        return Answer(text=f"{NO_EVIDENCE_MESSAGE}\n{SCOPE_NOTICE}", no_evidence=True)

    with ThreadPoolExecutor(max_workers=2) as pool:      # 답변 생성과 자격 체크리스트 추출을 동시에
        answer_job = pool.submit(_judge, question, evidence, needs)
        checklists = pool.submit(_extract_checklists, evidence).result()
        out = answer_job.result()
    recs = _top_or_none(_recommendations(question, evidence, needs, asked, out, checklists, region), evidence)
    if not recs and isinstance(out.get("services"), list) and _named_candidates(evidence, asked):
        # 후보가 있는데 판정에서 모두 빠졌으면 판정을 한 번 더 받는다. 같은 근거로도 판정이 흔들렸다
        # ('대학생 자취방 주거비' 질문: 같은 후보 5개로 6번 중 2번 근거 없음, 평가 q07).
        # 근거 없음이 맞는 질문은 매번 관련 없음으로 판정돼 다시 물어도 그대로였다 (q32~q34, 6번 모두).
        out = _judge(question, evidence, needs)
        recs = _top_or_none(_recommendations(question, evidence, needs, asked, out, checklists, region), evidence)
    if not recs:
        return Answer(text=f"{NO_EVIDENCE_MESSAGE}\n{SCOPE_NOTICE}", no_evidence=True)
    for r in recs[config.MAX_RECOMMENDATIONS:]:
        trace.decision(r.service_id, r.service_name, "제외", f"안내는 {config.MAX_RECOMMENDATIONS}개까지 (순서에서 밀림)")
    recs = recs[:config.MAX_RECOMMENDATIONS]   # 판정 후보는 JUDGE_CANDIDATES개, 안내는 앞 5개 (뒤로 보낸 제도가 먼저 잘린다)
    text = _render(recs, _str(out.get("situation")), _str(out.get("follow_up")))
    return Answer(text=text, recommendations=recs)


def _judge(question: str, evidence: dict[str, list[Chunk]], needs: list[str]) -> dict:
    """제도별 판정을 LLM에 받는다. 제도마다 따로 묻고(동시에) 결과를 한 출력으로 합친다. 모두 실패하면 빈 dict.

    5개를 한 번에 물으면 첫 제도만 제대로 보고 나머지를 '지원대상 정보가 부족'하다며 '관련 없음'으로 넘겼다:
    '부산 33살 직장인 주거 지원'에서 청년(19~39세) 대상이 적힌 매입임대·전세임대가 5번 모두 관련 없음, 따로 물으면
    2번 모두 확인 필요·도움 맞음 (평가 q29). 따로 물으니 같은 제도를 여러 번 적고 다른 제도를 빠뜨리는 꼬임
    (대전 출산 질문)도 생기지 않는다. 판정이 빠진 제도만 한 번 더 묻는다.
    situation은 첫 제도(가장 관련 높은 근거) 판정의 것, follow_up은 비어 있지 않은 첫 판정의 것을 쓴다."""
    sids = list(evidence)
    if not sids:
        return {}
    with ThreadPoolExecutor(max_workers=min(len(sids), MAX_JUDGE_WORKERS)) as pool:
        outs = dict(zip(sids, pool.map(lambda sid: _judge_one(question, sid, evidence[sid], needs), sids)))
    missing = [sid for sid in sids if outs[sid] is not None and not _services_for(outs[sid], sid)]
    for sid in missing:                   # 판정이 빠진 제도만 한 번 더 (드물어서 차례로)
        retry = _judge_one(question, sid, evidence[sid], needs)
        if retry is not None and _services_for(retry, sid):
            outs[sid] = retry
    ok = [outs[sid] for sid in sids if outs[sid] is not None]
    if not ok:
        return {}
    if not any(isinstance(o.get("services"), list) for o in ok):
        return ok[0]                      # 판정 형식이 아님: 판정 없이 원문으로 안내 (_recommendations)
    return {"situation": ok[0].get("situation", ""),
            "services": [s for sid in sids if outs[sid] is not None for s in _services_for(outs[sid], sid)],
            "follow_up": next((o.get("follow_up") for o in ok if o.get("follow_up")), "")}


MAX_JUDGE_WORKERS = 10  # 제도별 판정·체크리스트 동시 호출 수 (판정 후보 수 JUDGE_CANDIDATES와 같게)


def _top_or_none(recs: list[Recommendation], evidence: dict[str, list[Chunk]]) -> list[Recommendation]:
    """검색 상위 MAX_RECOMMENDATIONS개 후보 중 하나라도 통과했을 때만 그 뒤 후보(6~10위)로 빈자리를 채운다.
    상위가 모두 관련 없으면 근거 없음이 맞는 질문일 가능성이 높다: 후보를 10개로 늘리자 '기초연금 신청' 질문(범위 밖,
    q33)에 노인일자리가 2번 중 1번 안내됐다. 뒤 후보만 통과한 경우는 안내하지 않는다."""
    top = set(list(evidence)[:config.MAX_RECOMMENDATIONS])
    return recs if any(r.service_id in top for r in recs) else []


def _judge_one(question: str, sid: str, chunks: list[Chunk], needs: list[str]) -> dict | None:
    """제도 하나의 판정 (LLM 1번). 호출이 실패하면 None."""
    try:
        return condition_extractor._chat_json(SYSTEM_PROMPT, _build_prompt(question, {sid: chunks}, needs))
    except Exception as e:  # 네트워크·키·JSON 오류
        log.warning("판정 실패(%s), 이 제도는 판정 없이 처리: %s", sid, e)
        return None


def _services_for(out: dict, sid: str) -> list[dict]:
    """제도 하나만 물은 출력에서 그 제도의 판정. id를 잘못 적었어도 판정이 하나뿐이면 그 제도의 것으로 본다
    (실제 id는 숫자로만 돼 있어 LLM이 바꿔 적을 수 있다)."""
    services = [s for s in out.get("services") or [] if isinstance(s, dict)]
    own = [s for s in services if str(s.get("service_id")).strip() == sid]
    if not own and len(services) == 1:
        own = [{**services[0], "service_id": sid}]
    return own


def _recommendations(question: str, evidence: dict[str, list[Chunk]], needs: list[str], asked: list[str] | None,
                     out: dict, checklists: dict, region: str | None = None) -> list[Recommendation]:
    """판정(out)을 코드로 검증해 안내할 제도만 남긴다."""
    # LLM이 판정을 돌려줬으면 판정에 없는 제도는 관련 없다고 보고 뺀다.
    # 판정 자체가 없을 때(호출 실패·형식 오류)만 모든 제도를 근거 원문으로 안내한다.
    # 원하는 도움을 주는 제도인지는 구조화된 값(meets_need)으로 받아 코드로 적용한다
    # (applicable의 '관련 없음' 규칙만으로는 지역·나이만 맞는 제도가 섞였음: 공연 관람비 질문에 청년월세)
    judged = isinstance(out.get("services"), list)
    # 실제 서비스ID는 숫자로만 돼 있어('568000000347') LLM이 JSON 숫자로 돌려주기도 한다 -> 문자열로 맞춘다.
    # 같은 제도를 여러 번 적으면 첫 판정을 쓴다 (나중 것이 덮어써 '확인 필요'였던 국민취업지원제도가 '관련 없음'으로 빠졌다)
    by_id: dict[str, dict] = {}
    for s in out.get("services") or []:
        if isinstance(s, dict):
            by_id.setdefault(str(s.get("service_id")).strip(), s)
    ranked: list[tuple[tuple, Recommendation]] = []
    rescued_only = True                   # 통과한 제도가 모두 확인 안 된 '해당 없음'에서 살린 것뿐인가
    # 시·도를 직접 말하지 않아도('수원에 살아요') 조건 추출이 지역을 알아냈으면 지역을 말한 질문이다 (지자체 제도를 뒤로 미루지 않는다)
    no_region = region is None and condition_extractor._find_region(question) is None
    notes: dict[str, str] = {}            # 추적용: 제도별 선정 이유 한 줄 (trace가 꺼져 있어도 만든다. 비용은 문자열 몇 개)
    trace.title("제도별 판정과 거르기")
    named = _named_candidates(evidence, asked)
    for sid in evidence:
        if sid not in named:
            trace.decision(sid, evidence[sid][0].service_name, "제외", "특정 제도 이름을 물었는데 이름이 다름")
    for rank, (sid, chunks) in enumerate(named.items()):
        name = chunks[0].service_name
        gen = by_id.get(sid)
        if gen is None:
            if judged:
                trace.decision(sid, name, "제외", "판정 결과에 없음 (관련 없다고 보고 뺌)")
                continue
            gen = {}
        applicable = gen.get("applicable")
        unverified = applicable == NOT_ELIGIBLE and not _verified_mismatch(question, gen.get("mismatch"))
        if unverified:
            applicable = UNCLEAR          # 나이·거주지가 실제로 다르다고 확인되지 않으면 제외하지 않고 확인 필요로 남긴다
        if applicable in (NOT_ELIGIBLE, NOT_RELEVANT):
            mismatch = gen.get("mismatch") if isinstance(gen.get("mismatch"), dict) else {}
            trace.decision(sid, name, "제외", f"판정 '{applicable}'" + (f" (질문 '{mismatch.get('said')}' / 제도 '{mismatch.get('condition')}')" if mismatch else ""))
            continue
        if needs and gen.get("meets_need") is False:
            trace.decision(sid, name, "제외", f"원하는 도움({', '.join(needs)})을 주지 않는 제도 (meets_need=false)")
            continue                      # 원하는 도움을 주지 않는 제도 (지역·나이만 맞는 제도가 섞이는 것 방지)
        if _tenure_mismatch(asked, chunks):
            trace.decision(sid, name, "제외", "전세·월세를 묻는데 집을 사는 제도 (또는 그 반대)")
            continue                      # 전세·월세를 묻는데 집을 사는 제도 (또는 그 반대)
        if _other_group(question, name):
            # 특정 대상(장애인·보훈·북한이탈주민 등) 전용 제도인데 질문에 그 대상이 없다. 뒤로 미루기만 했더니 다른 제도가
            # 빠져 후보가 줄면 해당할 가능성이 낮은 이 제도가 안내에 올라왔다 -> 안내하지 않는다.
            trace.decision(sid, name, "제외", "특정 대상 전용 제도인데 질문에 그 대상이 없음 (해당 가능성 낮음)")
            continue
        # 신분(중고등 신입생·보훈대상)이 맞지 않는다는 판정은 코드로 확인할 수 없어 빼지 않지만 맨 앞 카드가 되지 않게
        # 뒤로 보낸다. 질문에 없는 특정 대상 전용 제도도 같다: 졸업 후 미취업 질문에 '장애인 취업성공패키지'가
        # 1번 카드였다 (평가 q02, 판정은 장애 여부를 말하지 않아 '확인 필요'로 맞게 냈고 검색 순위가 1위였다)
        closed = _new_closed(sid, chunks)
        # 이번 회차가 끝난 반복 모집 제도('이번 신청 마감 · 다음 공고 확인')도 지금 신청할 수 있는 제도 뒤로
        # (검수: q08 국가장학금 Ⅱ유형 2학기 9월 마감·'연1회(8월말~9월초)' 장학금이 앞자리에 나와 bad·기한 지남)
        flags = [label for on, label in (
            (unverified, "해당 없음 판정을 확인하지 못해 확인 필요로 남김"), (bool(closed), "신규 신청 중단 구절"),
            (_change_notice(question, name), "변경 신청 안내"),
            (_recurring_closed(_application_period(sid), date.today()), "이번 회차 마감"),
            (_unasked_job(question, needs, name), "일을 묻지 않았는데 일자리 제도")) if on]
        later = bool(flags)
        # 안내 순서 (판정 후보 10개 중 앞 5개만 나가므로): 판정을 통과한 정답이 검색 순서로만 정하면 잘렸다
        # (q12 '한국자산관리공사 채무조정'·q09 판정 '가능'인 학자금대출이 6위 아래). 지역을 말하지 않은 질문에 지자체 제도
        # (전주·세종·강원)가 앞자리를 차지했다 -> 뒤로 보낸 제도 맨 뒤, 지역 없는 질문의 지자체 제도는 전국 제도 뒤,
        # 판정 '가능'을 '확인 필요' 앞. 같은 조건 안에서는 검색 순서 (CLAUDE.md B 계약: 지역 없는 질문은 지자체 제도를 뒤로)
        local_later = no_region and _is_local(chunks)
        key = (later, local_later, applicable != LIKELY, rank)
        rec = _to_recommendation(question, chunks, gen, _met_asked(chunks, needs, asked)
                                 if gen.get("meets_need") is True else None, checklists.get(sid), asked)
        notes[sid] = (f"판정 '{applicable}', 원하는 도움 {'충족' if gen.get('meets_need') is True else '확인 안 됨'}; 이유: {rec.reason}"
                      + (f"; 뒤로 미룸: {', '.join(flags)}" if flags else "") + ("; 지역 없는 질문의 지자체 제도라 뒤로" if local_later else "")
                      + f"; 검색 순위 {rank + 1}")
        if closed:
            rec.needs_check.insert(0, f"{NEW_CLOSED_ITEM} — {closed} (원문 확인)")
        ranked.append((key, rec))
        rescued_only = rescued_only and unverified and _said_in_question(question, gen.get("mismatch"))
    # 확인 안 된 '해당 없음'을 살리는 것은 다른 제도와 함께 안내할 때만: 질문 구절을 근거로 해당 없다고 본 제도만
    # 남으면 판정이 해당 없다고 본 제도로만 답하게 된다. '기초연금 신청'(범위 밖, q33)에 '65세이상 기초연금수급자'
    # 대상인 노인일자리가 '해당 없음'+meets_need true로 남아 단독 카드가 됐다 (8번 중 4번, 판정 재시도로 기회가 두 번).
    # 질문에 없는 구절을 근거로 댄 '해당 없음'은 판정 자체를 믿을 수 없어 혼자여도 확인 필요로 남긴다.
    if ranked and rescued_only:
        trace.out("  -> 질문 구절을 근거로 '해당 없음'이라 본 제도만 남아 모두 안내하지 않습니다 (근거 없음 처리)")
        return []
    ordered = [r for _, r in sorted(ranked, key=lambda kr: kr[0])]
    if trace.enabled():
        trace.title("안내 후보 순서 (앞 5개를 안내)")
        for pos, r in enumerate(ordered, start=1):
            trace.out(f"  {pos}. {r.service_id}  {trace.short(r.service_name)}  <- {notes.get(r.service_id, '')}")
    return ordered


NEW_CLOSED_ITEM = "신규 신청"
# 신청방법·신청기한 원문의 신규 모집 중단 구절 ('* '24년부터 신규지원 중단', '2017년 이후 출생아 신규가입 불가').
# 제도 전체가 끝났는지 일부 대상만 끝났는지('제대군인 신규 신청 불가', '주거임차비의 경우 신규 신청 불가')는 코드로
# 가를 수 없어 빼지 않고, 카드 순서를 뒤로 보내고 확인 필요 맨 앞에 원문 구절을 보인다 (q10 청년내일채움공제)
_NEW_CLOSED_RE = re.compile(r"[^\n.·○※*]{0,25}신규\s*(?:지원|가입|신청|모집|접수)?\s*(?:이\s*)?(?:중단|불가|종료)"
                            r"|신규[^\n/※*]{0,20}?모집\s*(?:완료|마감)")   # '신규계약: (상) 1월 (하) 7월 -모집완료'


def _new_closed(sid: str, chunks: list[Chunk]) -> str:
    """신청방법·신청기한 원문에 신규 모집 중단 구절이 있으면 그 구절(원문 그대로), 없으면 ''."""
    texts = [citation._body_text(c) for c in chunks if c.section == "신청방법"] + [_application_period(sid)]
    m = next((m for t in texts if t for m in [_NEW_CLOSED_RE.search(t)] if m), None)
    return " ".join(m.group().split()).strip(" -:,") if m else ""


def _is_local(chunks: list[Chunk]) -> bool:
    """지자체 제도인가: 소관기관으로 시·도를 알 수 있거나(산하 기관 포함), 저장된 지역 값이 전국이 아닌 시·도."""
    meta = chunks[0].metadata if chunks else {}
    region = normalize_region(meta.get("region") or "")
    name = chunks[0].service_name if chunks else ""
    return _agency_region(meta.get("agency") or "", name) is not None or region not in (None, "전국")


# 특정 대상 전용 제도의 이름 낱말 -> 질문에 있으면 그 대상으로 보는 말. '저소득·수급자'는 주거급여처럼 넓은 제도에도
# 붙어 넣지 않는다
_GROUP_WORDS = {"장애": ("장애",), "보훈": ("보훈", "유공"), "국가유공": ("보훈", "유공"), "북한이탈": ("북한", "탈북"),
                "탈북": ("북한", "탈북"), "한부모": ("한부모", "미혼모", "미혼부"), "다문화": ("다문화", "이민", "외국"),
                "결혼이민": ("다문화", "이민", "외국"), "어르신": ("어르신", "노인", "65세"), "노인": ("어르신", "노인", "65세")}


# 이미 선정된 사람의 변경 신청 안내 ('(변경·중지·연장 등) 부산 평생함께 청년 주거비', '청년월세 지원사업 변경신청').
# 새로 신청하려는 질문에는 제도가 아니라 사후 절차라 근거 자리를 차지하지 않게 뒤로 보낸다 (q29: 5자리 중 2번째)
_CHANGE_NOTICE_RE = re.compile(r"변경\s*신청|변경·중지|중지·연장")
_ASKS_CHANGE_RE = re.compile(r"변경|이사|전입|중지|연장|해지|바뀌")


def _change_notice(question: str, service_name: str) -> bool:
    """제도 이름이 변경 신청 안내인데 질문은 변경을 묻지 않는가."""
    return bool(_CHANGE_NOTICE_RE.search(service_name or "")) and not _ASKS_CHANGE_RE.search(question or "")


# 사람을 뽑는 일자리 제도 이름. 원문의 '다자녀보육가구 우대'·'저소득층 우대'로 처지가 맞아 보여 일을 묻지 않은
# '서울 중구 다자녀가구 혜택' 질문에 통계조사원 채용이 1·2번 카드였다 (우대는 혜택이 아니라 선발 가점)
_JOB_NAME_RE = re.compile(r"일자리|조사원|채용|공공근로")
_ASKS_JOB_RE = re.compile(r"일자리|취업|구직|일하|일을|근로|채용|알바|아르바이트|직장|재직|경력|면접|실직|퇴사|구직|생계")


def _unasked_job(question: str, needs: list[str] | None, service_name: str) -> bool:
    """일을 묻지 않은 질문에 나온 일자리 제도인가 (빼지 않고 뒤로 보낸다). 처지(needs의 '구직(…)')도 본다."""
    asked_text = " ".join([question or "", *(needs or [])])
    return bool(_JOB_NAME_RE.search(service_name or "")) and not _ASKS_JOB_RE.search(asked_text)


def _other_group(question: str, service_name: str) -> bool:
    """제도 이름이 특정 대상(장애인·보훈·북한이탈주민 등) 전용인데 질문에 그 대상이 없는가."""
    q = "".join((question or "").split())
    return any(w in (service_name or "") and not any(s in q for s in said) for w, said in _GROUP_WORDS.items())


# 집에 사는 방식: 세 들어 살기(보증금·전세·월세·임대) / 사기(구입·구매·매매). '매입임대'는 공공이 사서 빌려주는 임대다.
_RENT_RE = re.compile(r"보증금|전세|월세|전월세|임차|임대")
_BUY_RE = re.compile(r"구입|구매|매매|내\s*집\s*마련|분양")


def _tenure_mismatch(asked: list[str] | None, chunks: list[Chunk]) -> bool:
    """찾는 도움과 제도의 주거 방식이 반대인가 (코드 검증). 한쪽만 물었고 제도 원문에는 반대쪽 말만 있을 때 True.
    '보증금 대출' 질문에 '구매용도만 가능'한 내집마련 디딤돌 대출이 meets_need=true로 안내됐다 (평가 q06, 2회 모두
    근거 충실도에서 걸림). 원문에 두 쪽 말이 다 있거나 어느 쪽도 없으면 판단하지 않는다."""
    said = " ".join(asked or [])
    wants_rent, wants_buy = bool(_RENT_RE.search(said)), bool(_BUY_RE.search(said))
    if wants_rent == wants_buy:
        return False
    body = " ".join(c.text for c in chunks)
    has_rent, has_buy = bool(_RENT_RE.search(body)), bool(_BUY_RE.search(body))
    return (has_buy and not has_rent) if wants_rent else (has_rent and not has_buy)


def _named_candidates(evidence: dict[str, list[Chunk]], asked: list[str] | None) -> dict[str, list[Chunk]]:
    """특정 제도를 이름으로 물었으면 그 제도만 남긴다 ('청년희망적금' 질문에 청년미래적금, 평가 q30)."""
    named = _named_programs(asked)
    if not named:
        return evidence
    return {sid: chunks for sid, chunks in evidence.items()
            if any(n in v for n in named for v in _name_variants(chunks[0].service_name))}


# 제도 이름처럼 끝나는 말 (상품·급여 이름). '지원금'·'대출'은 '청년지원금'·'청년전용대출'처럼 일반 표현에도 쓰여 넣지 않는다.
_PROGRAM_SUFFIX_RE = re.compile(r"(?:적금|통장|계좌|공제|수당|장려금|바우처|장학금|패스)$")
MIN_PROGRAM_NAME_CHARS = 5    # '청년적금'(4자)처럼 짧은 것은 일반 표현으로 본다


def _squash_name(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s or "")


def _name_variants(service_name: str) -> list[str]:
    """서비스명과, 가운뎃점으로 묶인 이름을 하나씩 푼 이름들 (글자만 남김).
    '근로·자녀장려금' -> ['근로자녀장려금', '근로장려금', '자녀장려금']. 묶인 이름을 다른 제도로 보고 빼던 문제
    ('근로장려금' 질문에 근로·자녀장려금이 1위로 검색됐는데 근거 없음, 범위 확대 색인 2026-10-22)."""
    variants = [_squash_name(service_name)]
    for m in re.finditer(r"([가-힣]{1,6})·([가-힣]{1,6})", service_name):
        a, b = m.group(1), m.group(2)
        # '·자녀'를 빼면 '근로장려금'. 뒤 낱말('자녀장려금')의 경계를 모르므로 앞 글자를 하나씩 빼 본다
        # ('근로녀장려금' 같은 변형도 생기지만 질문의 제도 이름과 맞을 일이 없어 무해하다)
        for k in range(1, len(b)):
            variants.append(_squash_name(service_name[:m.start(2) - 1] + b[k:] + service_name[m.end(2):]))
        variants.append(_squash_name(service_name[:m.start(1)] + service_name[m.start(2):]))   # '근로·'를 빼면 '자녀장려금'
    return [v for v in variants if v]


def _named_programs(asked: list[str] | None) -> list[str]:
    """질문이 이름으로 콕 집어 물은 제도 ('청년희망적금', '국가장학금'). 찾는 도움 구절의 낱말 중 제도 이름처럼 끝나는
    5자 이상 낱말 ('청년도약계좌 가입 조건' -> '청년도약계좌').

    LLM은 '특정 제도를 묻는데 근거에 없으면 관련 없음' 규칙이 있어도 이름이 비슷한 다른 제도(청년미래적금)를
    도움이 된다고 판정했다 (q30, 3회 모두). 그래서 이름을 물었으면 서비스명에 그 이름이 있는 제도만 남긴다 (코드 검증).
    처음에는 구절 전체가 한 낱말일 때만 봐서 '청년도약계좌 가입 조건'(q25)에 청년미래적금이 나왔다."""
    out = []
    for a in asked or []:
        for word in a.split():
            name = _JOSA_RE.sub("", word.strip())
            if (len(name) >= MIN_PROGRAM_NAME_CHARS and _PROGRAM_SUFFIX_RE.search(name)
                    and name not in condition_extractor._GENERIC_HELP and _squash_name(name) not in out):
                out.append(_squash_name(name))
    return out


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
    phrases = [a for a in asked if _asked_in(a, body)]
    situations = [n.split("(")[0] for n in needs if n not in asked]
    return phrases or [f"{s} 상황" for s in situations] or asked


def _asked_in(phrase: str, body: str) -> bool:
    """찾는 도움 구절('월세 지원')의 핵심 낱말('월세')이 제도 원문(기호·공백을 뺀 body)에 있는가.
    2자 이상이고 '지원·제도'처럼 어디에나 붙는 말이 아닌 낱말만 본다."""
    words = (_JOSA_RE.sub("", w) for w in re.findall(r"[0-9A-Za-z가-힣]+", phrase))
    return any(len(w) >= 2 and w not in condition_extractor._GENERIC_HELP and w in body for w in words)


def _build_prompt(question: str, evidence: dict[str, list[Chunk]], needs: list[str] | None = None) -> str:
    blocks = []
    for sid, chunks in evidence.items():
        c0, meta, sections = chunks[0], _merged_meta(chunks), _sections(chunks)
        lines = [f"[근거 id={sid}] {c0.service_name}",
                 f"- 소관기관: {meta.get('agency') or '정보 없음'}",
                 f"- 지역: {meta.get('region') or '정보 없음'}",
                 f"- 지역 출처: {meta.get('region_provenance') or 'legacy_unknown'} (검색용 지역 단서; 기관 추정·출처 미상 값은 검증된 거주 자격이 아니며 지원대상·선정기준 원문 확인)",
                 f"- 연령: {_age_text(meta)}",
                 f"- 신청기한: {meta.get('deadline') or '상시 또는 정보 없음'}"]
        for s in SECTIONS:
            lines.append(f"- {s}: {sections.get(s, '(자료 없음)')}")
        blocks.append("\n".join(lines))
    head = f"[사용자 질문]\n{question}\n\n"
    if needs:
        head += "[사용자가 원하는 도움]\n" + "\n".join(f"- {n}" for n in needs) + "\n\n"
    return head + "\n\n".join(blocks)


def _to_recommendation(question: str, chunks: list[Chunk], gen: dict, asked: list[str] | None = None,
                       checklist: list[dict] | None = None, help_: list[str] | None = None) -> Recommendation:
    """LLM 출력이 없거나 빠진 항목은 조각 원문으로 채운다.

    asked: 이 제도가 도와준다고 판정된(meets_need) '찾는 것' 구절. 나이·거주지 짝이 없을 때 일반 문구 대신 쓴다
    (실제 데이터: 나이·지역 없이 '면접 비용 지원'만 물으면 모든 카드의 이유가 같은 일반 문구였다).
    checklist: 자격 조건만 따로 뽑은 결과(_extract_checklists). 있으면 확인 필요 조건(needs_check)으로 쓰고,
    없으면 답변 생성의 needs_check를 쓴다. 어느 쪽이든 기준 구절은 원문에 있는 것만 남긴다.
    help_: 질문의 '찾는 도움' 구절 전체 (자격 조건 짝에서 빼는 데 쓴다)."""
    meta = _merged_meta(chunks)
    raw = _sections(chunks)
    llm_pairs = _grounded_matches(question, chunks, gen.get("matches"), help_)
    code_pairs = _code_matches(question, chunks)
    said, conds = _merge_pairs(code_pairs, llm_pairs)
    if said:
        reason = f"말씀하신 {', '.join(f'‘{s}’' for s in said)} 조건이 이 제도의 대상" \
                 + (f"({', '.join(f'‘{c}’' for c in conds)})" if conds else "") + "에 해당될 수 있어요."
    elif asked:
        reason = f"말씀하신 {', '.join(f'‘{a}’' for a in asked)}에 도움이 될 수 있는 제도예요. 자격은 아래 조건을 확인해 주세요."
    else:
        reason = GENERIC_REASON

    needs = _needs_with_criteria(checklist, chunks, sections=TARGET_SECTIONS, keep_names=True) if checklist else []
    if needs:     # 사용자가 말해서 코드로 충족이 확인된 나이·거주지 조건은 체크할 필요가 없다
        needs = _drop_satisfied(needs, code_pairs[1])
    else:         # 체크리스트를 못 뽑았거나 원문에서 하나도 확인되지 않으면 답변 생성의 확인 필요 조건을 쓴다
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

    how = _llm_or_raw(gen.get("how_to_apply"), raw.get("신청방법"))
    deadline = meta.get("deadline")
    period = _application_period(chunks[0].service_id)
    start = _period_start(period)
    if start and start > date.today() and not _OPEN_PERIOD_RE.search(period):
        period = f"{UPCOMING_MARK} · {period}"     # 아직 접수 전인 제도 (마감 지난 1회성 제도는 _drop_expired가 이미 뺐다)
    elif _recurring_closed(period, date.today()):
        period = f"{CLOSED_RECURRING_MARK} · {period}"   # 이번 신청은 끝났고 다시 모집하는 제도
    how += f" / 신청기한: {period or (f'{deadline}까지' if deadline else '상시 또는 정보 없음')}"

    return Recommendation(
        service_name=chunks[0].service_name,
        agency=meta.get("agency") or citation.UNKNOWN_AGENCY,
        reason=reason,
        benefit=_llm_or_raw(gen.get("benefit"), raw.get("지원내용")),
        how_to_apply=how,
        needs_check=needs,
        citations=citation.build_citations(chunks),
        service_id=chunks[0].service_id,     # 카드에서 판정 기준 문서를 찾을 때 쓴다 (guide_evidence.find_guides)
        benefit_detail=benefit_detail.extract(chunks),        # W2 자리 (src/generate/benefit_detail.py)
        apply_url=apply_link.find(chunks, how),               # W3 자리 (src/generate/apply_link.py)
        period_detail=period_detail.explain(period),          # W4 자리 (src/generate/period_detail.py)
    )


_SAYS_MISSING_RE = re.compile(r"(?:자료|정보|내용)(?:에|이|가|은|는|에는)?\s*없")


def _llm_or_raw(llm_text, raw_text: str | None) -> str:
    """LLM 요약을 쓰되, 요약이 없거나 '자료에 없음'이라고 했는데 원문 항목이 있으면 원문을 쓴다 (코드 검증).
    근거에 신청방법('방문, 인터넷')이 짧게 있으면 LLM이 '신청방법은 자료에 없음'이라고 써서 근거 충실도에서
    빠졌다 (평가 q13, 2회 모두)."""
    text = _str(llm_text)
    if raw_text and (not text or _SAYS_MISSING_RE.search(text)):
        return raw_text
    return text or raw_text or NOT_IN_EVIDENCE


MAX_PERIOD_CHARS = 120   # 카드에 보여 줄 신청기한 원문 최대 길이 (공고 안내 문단이 통째로 붙는 것 방지)
UPCOMING_MARK = "신청 예정"   # 신청 시작일이 아직 오지 않은 제도의 신청기한 앞에 붙인다 (시행 예정 제도도 안내하되 구분)
CLOSED_RECURRING_MARK = "이번 신청 마감 · 다음 공고 확인"   # 학기·해마다 반복되는 제도의 이번 회차가 끝났을 때


def _application_period(service_id: str) -> str:
    """제도의 신청기한 원문 (A 저장소의 application_period, 예: '2026년 5월 22일~6월 22일', '수시신청(예산소진시까지)').

    검색 조각에는 날짜로 해석된 deadline만 있어(상시·'접수기관별 상이'·기간 표현은 None) 카드 대부분이
    '상시 또는 정보 없음'이었다. 원문은 A의 DB에만 있어 A의 공개 조회 함수(store.get_record)로 읽는다.
    DB가 없으면(샘플 미리보기·테스트) 부르지 않는다: get_record는 DB 파일이 없으면 새로 만든다.
    읽기에 실패하면 빈 문자열 (예전처럼 deadline으로 표시)."""
    if not Path(config.DB_PATH).exists():
        return ""
    try:
        record = ingest_store.get_record(service_id)
    except Exception as e:  # 잠금·손상 등: 답변은 계속 만든다
        log.warning("신청기한 원문 조회 실패(%s): %s", service_id, e)
        return ""
    period = " ".join(_str(((record or {}).get("payload") or {}).get("application_period")).split())
    return period if len(period) <= MAX_PERIOD_CHARS else period[:MAX_PERIOD_CHARS - 1] + "…"


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


def _grounded_matches(question: str, chunks: list[Chunk], matches,
                      help_: list[str] | None = None) -> tuple[list[str], list[str]]:
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
        if any(h and (squash(s) in squash(h) or squash(h) in squash(s)) for h in help_ or []):
            continue                      # 찾는 도움 구절은 자격 조건이 아니다 ('보증금을 빌려주거나 보증해 주는 제도'
            #                               조건이 대상 '무주택세대구성원'에 해당, 평가 q22 Faithfulness)
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


TARGET_SECTIONS = ("지원대상", "선정기준")   # 자격 조건이 적힌 항목


def _drop_satisfied(needs: list[str], verified: list[str]) -> list[str]:
    """코드가 질문과 짝지어 충족을 확인한 나이·거주지 조건(_code_matches의 근거 구절)과 같은 체크 항목을 뺀다.
    '연령 — 만 19~34세 이하'는 27살이라고 말했으면 이미 맞으므로 체크할 필요가 없다. 나이·거주지 항목만 뺀다
    ('만 19~34세 무주택자'처럼 다른 조건이 섞인 구절을 통째로 빼지 않게)."""
    squash = lambda s: re.sub(r"[^0-9A-Za-z가-힣%~]", "", s)  # noqa: E731
    done = [squash(v) for v in verified if v]
    def satisfied(n: str) -> bool:
        label, sep, crit = n.partition(" — ")
        return bool(sep) and label.split()[-1] in ("연령", "거주지") and any(     # '청년 연령'처럼 유형이 붙어도
            squash(crit) in d or d in squash(crit) for d in done)
    return [n for n in needs if not satisfied(n)]


def _needs_with_criteria(items, chunks: list[Chunk], sections: tuple[str, ...] | None = None,
                         keep_names: bool = False) -> list[str]:
    """확인 필요 조건에 근거 원문의 기준을 붙인다: '소득 — 기준 중위소득 150% 이하'.

    이름만 있으면('소득 기준') 사용자가 결국 원문을 열어 봐야 했다. 기준 구절은 근거 원문에 글자 그대로 있을 때만
    붙인다 (LLM이 금액·비율을 지어내거나 바꿔 쓰는 일 차단, 이유 문장의 matches와 같은 원리).
    기준 구절이 없으면 그 조건 이름이 원문에 나올 때만 '(기준은 원문 확인)'을 붙여 남기고, 원문에 없는 조건은 뺀다.

    keep_names: 조건 이름을 LLM이 준 그대로 쓴다 (자격 체크리스트 추출 결과). 답변 생성 LLM은 이름이 자주 어긋나
    기준 구절 내용으로 이름을 다시 붙이지만(_criterion_label), 그 규칙은 '6세이하 자녀를 둔 한부모가족'을 연령으로,
    '관내 대학 재학생'을 거주지로 바꿨다. 자격만 뽑는 호출은 이름을 조건별로 정확히 줘서 그대로 둔다."""
    # 글자·숫자·%만 남겨 비교한다: 원문의 각주 표시('원가구*의')·괄호·따옴표를 LLM이 빼고 옮겨도 같은 구절로 본다
    # (내용은 정확한데 '*' 하나 때문에 기준이 빠졌다). 금액·비율 숫자는 그대로 검증된다.
    squash = lambda s: re.sub(r"[^0-9A-Za-z가-힣%]", "", s)  # noqa: E731
    body = squash(" ".join(citation._body_text(c) for c in chunks if sections is None or c.section in sections))
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
            text = f"{name if keep_names else _criterion_label(crit, name)} — {crit}"
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
        hits = [_criterion_core(p, word) for p in pieces
                if word in p and _AMOUNT_RE.search(p) and len("".join(p.split())) <= MAX_CRITERION_CHARS]
        hits = [h for h in hits if h]
        if hits:
            out.append(f"{word} — {', '.join(dict.fromkeys(hits[:2]))}")
        else:
            out.append(f"{word} 기준 (자료에 기준 금액 없음)")
    return out


# 금액·비율 하나 ('1억 3천만원'처럼 단위가 이어지면 함께), 뒤에 붙은 글자까지
_AMOUNT_SPAN_RE = re.compile(r"\d[\d,.]*\s*(?:천|만|억|원|%|퍼센트|구간|분위)(?:\s*\d[\d,.]*\s*(?:천|만|억|원)+)*[^\s,(]*"
                             r"(?:\([^()]{1,15}\))?")       # '70%(맞벌이 90%)'처럼 짧은 괄호는 닫는 데까지
_LIMIT_WORD_RE = re.compile(r"\s*(?:이하|이내|미만|초과|이상)(?:인|의)?(?:\s+(?:가구|자|세대)(?![가-힣]))?")
# 소득 낱말 앞에서 함께 남길 수식어 ('본인 연소득', '부부합산 연소득', '기준 중위 소득')
_INCOME_MODIFIER_RE = re.compile(r"^(?:본인|부부|부부합산|가구|가구당|월|연|월평균|기준|중위|도시근로자|원가구|독립가구)$")


def _criterion_core(piece: str, word: str) -> str:
    """소득·재산 구절에서 기준의 핵심만 남긴다: '…85㎡이하의 주택에서 임차하여 살고 있는 중위소득 180% 이내 가구'
    -> '중위소득 180% 이내 가구'. 체크리스트 한 줄이 문장 두 개를 이어 붙인 것처럼 길어졌다.
    '소득'이 든 낱말(앞의 '본인·부부합산·기준 중위' 같은 수식어 포함)부터 그 뒤 첫 금액·비율과 '이하·이내' 표현까지.
    금액이 없으면 ''."""
    i = piece.find(word)
    if i < 0:
        return ""
    start = piece.rfind(" ", 0, i) + 1                       # '중위소득'·'연소득'처럼 낱말 처음부터
    for _ in range(2):                                        # 앞 수식어는 두 낱말까지
        before = piece[:start].rstrip().rsplit(" ", 1)[-1] if start else ""
        if not (before and _INCOME_MODIFIER_RE.match(before)):
            break
        start = piece.rfind(before, 0, start)
    amount = _AMOUNT_SPAN_RE.search(piece, i)
    if not amount:
        return ""
    end = amount.end()
    limit = _LIMIT_WORD_RE.match(piece, end)
    if limit:
        end = limit.end()
    return piece[start:end].strip(" ,")


_AGE_RANGE_RE = re.compile(r"(\d+)\s*세?\s*[~∼\-–]\s*(?:만\s*)?(\d+)\s*세")
_AGE_MAX_RE = re.compile(r"(\d+)\s*세\s*(이하|미만)")
_AGE_MIN_RE = re.compile(r"(\d+)\s*세\s*(이상|초과)")


def _said_in_question(question: str, mismatch) -> bool:
    """'해당 없음' 판정이 댄 사용자 구절(said)이 질문 원문에 있는가."""
    said = _str(mismatch.get("said")).strip("‘’'\"") if isinstance(mismatch, dict) else ""
    return bool(said) and "".join(said.split()) in "".join(question.split())


def _verified_mismatch(question: str, mismatch) -> bool:
    """'해당 없음' 판정을 코드로 확인한다. 확인되지 않으면 빼지 않고 '확인 필요'로 남긴다.

    - said가 질문 원문에 있고, 나이나 거주지를 담은 구절이어야 한다
      (실제 데이터: 질문 문장 전체를 said로 써서 청년도약계좌 질문의 제도가 모두 빠짐).
    - 나이면 근거의 연령 범위를 코드가 읽어 실제로 벗어나는지 본다 ('27살' vs '18세~45세'를 해당 없음으로 판정한 적 있음).
    - 거주지면 근거 구절이 다른 시·도를 말하고 사용자의 시·도는 말하지 않아야 한다."""
    if not _said_in_question(question, mismatch):
        return False
    said = _str(mismatch.get("said")).strip("‘’'\"")
    cond = _str(mismatch.get("condition"))

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


# 진행 단계 알림: UI가 답변을 기다리는 동안 지금 단계를 보여 준다 (질문 하나에 10~20초).
# answer_question의 인자를 늘리지 않도록(B·문서의 인터페이스 유지) 문맥 변수로 콜백을 받는다. 설정하지 않으면 아무 일도 없다.
PROGRESS: ContextVar[Callable[[str], None] | None] = ContextVar("answer_progress", default=None)
STEP_CONDITIONS, STEP_SEARCH, STEP_GENERATE = "conditions", "search", "generate"


def _progress(step: str) -> None:
    callback = PROGRESS.get()
    if callback is None:
        return
    try:
        callback(step)
    except Exception as e:  # 화면 표시 실패로 답변을 멈추지 않는다
        log.warning("진행 단계 알림 실패: %s", e)


def answer_question(question: str) -> Answer:
    """온라인 전체 흐름: 조건 추출 -> 검색(필터 완화 재시도) -> 근거 선별 -> 생성. app은 이 함수만 호출한다.

    필터 완화(filters.relax) 재검색은 B의 hybrid.search_with_info 안에서 한다 (중복 구현하지 않음).
    """
    return _answer_with_evidence(question)[0]


def _answer_with_evidence(question: str) -> tuple[Answer, dict[str, list[Chunk]]]:
    """answer_question의 본체. 생성에 넘긴 근거도 함께 돌려준다 (답변 평가가 같은 흐름으로 채점하도록)."""
    _progress(STEP_CONDITIONS)
    conditions = condition_extractor.extract_conditions(question)
    filters = condition_extractor.to_filters(conditions)
    region = conditions.get("region")
    if trace.enabled():
        trace.title("질문 분석")
        trace.out(f"  질문: {question}")
        trace.out("  조건: " + (", ".join(f"{k}={v}" for k, v in conditions.items()) or "(없음)"))
    # CLAUDE.md: 완화 여부가 필요하면 search_with_info. search와 결과는 같고 완화 정보가 더 붙는다.
    # 지역을 말했으면 후보를 넉넉히 받아 다른 지역 지자체 제도를 뺀 뒤 상위 5개를 고른다 (_drop_other_regions)
    k = config.DEFAULT_TOP_K * REGION_CANDIDATE_FACTOR if region else config.DEFAULT_TOP_K
    # 시·군·구는 검색어에 그대로 둔다. 빼고 검색했더니 그 구의 맞는 제도(동대문구 미취업청년 자격취득 활동비,
    # 대전 서구 출산지원금)를 못 찾았다. 다른 구 제도는 검색 뒤 _drop_other_districts로 뺀다.
    districts = _find_districts(question, region)
    _progress(STEP_SEARCH)
    result = hybrid.search_with_info(question, filters, k=k)
    today = date.fromisoformat(filters.get("today") or date.today().isoformat())
    trace.listing("검색 결과", result.chunks)
    chunks = _drop_expired(result.chunks, today)
    trace.dropped("신청기한이 지남", result.chunks, chunks)
    if not region or districts:
        # 판정 후보(JUDGE_CANDIDATES)를 채우려고 더 큰 검색 결과를 뒤에 붙인다. 처음 검색의 순서는 그대로 둔다:
        # 처음부터 k를 키우면 B의 후보 수(k*5)가 늘어 상위 순위가 달라졌다 (면접 비용 질문에서 면접정장 제도들이
        # 6위 밖으로 밀려 근거 없음, 평가 q03).
        # 시·군·구까지 말했으면 더 크게 받는다: '서울 마포구 65세' 15개 중 12개가 다른 구 어르신 제도라 구 필터 뒤
        # 3개만 남았다 (50개면 11개. 검색 시간은 k와 거의 상관없음, 0.8초)
        more = hybrid.search_with_info(question, filters,
                                       k=DISTRICT_CANDIDATES if districts else config.DEFAULT_TOP_K * REGION_CANDIDATE_FACTOR)
        seen = {c.service_id for c in result.chunks}
        chunks += _drop_expired([c for c in more.chunks if c.service_id not in seen], today)
    for q in topical.phrase_queries(conditions.get("help"), question):
        # 질문 문장 전체로 검색하면 '정부나 지자체 지원제도가 있어?'·'수원에 살고 있어' 같은 말에 끌려 핵심 주제(대중교통비)와 맞는
        # 전국 제도(K-패스)가 64위로 밀렸다. 핵심어만으로 검색한 결과를 뒤에 더한다 (K-패스: 필터 적용 합산 2위).
        extra = _drop_expired(hybrid.search_with_info(q, filters, k=k).chunks, today)
        seen = {c.service_id for c in chunks}
        chunks += [c for c in extra if c.service_id not in seen]
        trace.listing(f"핵심어 '{q}'로 다시 찾은 제도", extra)
    before = chunks
    chunks, region_relaxed = _drop_other_regions(chunks, region)
    trace.dropped(f"다른 지역 제도 (질문 지역: {region})", before, chunks)
    before = chunks
    chunks = _drop_other_districts(chunks, districts, filters.get("field"), conditions.get("help"))
    trace.dropped(f"다른 시·군·구 제도 ({', '.join(sorted(districts))})", before, chunks)
    if region and not region_relaxed:
        national = _national_candidates(question, region, filters, today)
        trace.listing("지역 필터 없이 다시 찾은 전국 제도", national)
        chunks = _mix_national(chunks, national)
    # 상위 5개만 판정하면 관련 없는 제도가 빠진 자리가 비어 버렸다: 정답 손실 분석에서 검색 6~15위에 있던 정답 10개가
    # 판정 기회가 없었다 (q02 청년도전지원사업 13위 등). 10개를 판정하고(동시) 통과한 것 중 앞 5개만 안내한다
    hidden = catalog_store.hidden_ids()          # 제도 목록 관리 화면에서 숨긴 제도 (색인에 반영하기 전에도 바로 빠진다)
    if hidden:
        before = chunks
        chunks = [c for c in chunks if c.service_id not in hidden]
        trace.dropped("관리자가 숨긴 제도", before, chunks)
    chunks = exclusion.exclude_in_use(question, conditions, chunks)   # W1 자리 (src/generate/exclusion.py)
    chunks = topical.prioritize(chunks, conditions.get("help"))   # 핵심 주제어가 본문에 있는 제도를 판정 후보 앞으로
    selected = evidence_mod.select_evidence(chunks, JUDGE_CANDIDATES)
    trace.dropped(f"판정 후보 {JUDGE_CANDIDATES}개 밖", chunks, [c for cs in selected.values() for c in cs])
    trace.listing("판정에 넘긴 후보", [cs[0] for cs in selected.values()])
    _progress(STEP_GENERATE)
    answer = _generate(question, selected, _needs_from(conditions), conditions.get("help"), region)
    used = dict(result.filters_used)
    if region_relaxed:
        used.pop("region", None)
    notice = _relaxed_notice(filters, used) if (result.relaxed or region_relaxed) else ""
    if notice and not answer.no_evidence:
        answer.text = f"{notice}\n\n{answer.text}"
    return answer, selected


JUDGE_CANDIDATES = 10         # 판정에 넘기는 후보 제도 수 (안내는 그중 통과한 앞 config.MAX_RECOMMENDATIONS개)
REGION_CANDIDATE_FACTOR = 3   # 검색 후보 배수 (지역을 말했거나 기한 지난 제도가 빠졌을 때 5 -> 15개에서 5개를 고른다)
DISTRICT_CANDIDATES = 50      # 시·군·구까지 말한 질문의 추가 검색 수, 지역 질문의 전국 후보 추가 검색 수
MAX_LOCAL_SERVICES = 3        # 지역을 말했을 때 답변 5자리 중 그 지역 지자체 제도의 최대 수
MIN_NATIONAL_SERVICES = 2     # 지역을 말했을 때 전국 제도(중앙부처·공공기관)에 남겨 두는 최소 자리


def _national_candidates(question: str, region: str, filters: dict, today: date) -> list[Chunk]:
    """지역 낱말을 뺀 질문으로 다시 검색해 얻은 전국 제도 후보.

    질문에 '대전'이 있으면 검색이 대전 제도를 위로 올려, 범위 확대 색인(제도 10,603개)에서 '대전 사는데 곧 아이가 태어나요'의
    상위 15개 중 전국 제도는 11위 아래 3개뿐이었다 (출산급여·행복출산 같은 핵심 제도가 안내되지 못함).
    '대전'을 빼고 검색하면 상위가 전국 출산 제도였다.
    지역 필터도 뺀다: B는 지역이 없을 때만 지자체 제도를 뒤로 보내(config.LOCAL_PENALTY) 전국 제도가 먼저 오는데,
    지역 필터를 두면 그게 꺼져 지역 값이 빈 다른 지역 제도들이 후보를 채웠다 (경기 면접 질문). 결과에서 전국 제도만 쓰므로
    지역 필터는 필요 없다. 나이·기한 필터는 그대로 둔다."""
    stripped = " ".join(w for w in question.split()                       # 시·도와 시·군·구 낱말을 뺀다
                        if condition_extractor._find_region(w) != region and not _find_districts(w)).strip()
    # 낱말을 하나도 못 뺐어도('수원에 살고 있어'처럼 시·도 이름이 아니고 시·군·구로도 안 잡히는 말) 검색은 한다: 이 검색의
    # 목적은 지역 낱말 제거만이 아니라 지역 필터를 빼 전국 제도를 먼저 올리는 것이다. 예전에는 같은 질문이면 건너뛰어
    # '수원에 살아요 + 대중교통비' 질문에서 전국 제도(K-패스)가 후보에 하나도 들어오지 못했다.
    if not stripped:
        return []
    nationwide = {k: v for k, v in filters.items() if k != "region"}
    # 15개 순서를 먼저 두고 50개 결과에서 새로 나온 것을 뒤에 붙인다 (k를 키우면 B의 상위 순위가 바뀐다).
    # '서울 마포구 65세 어르신'은 지역 낱말을 빼도 상위 15개 중 전국 제도가 2개뿐이었다 (나머지는 다른 지역 어르신 제도)
    try:
        chunks = hybrid.search_with_info(stripped, nationwide, k=config.DEFAULT_TOP_K * REGION_CANDIDATE_FACTOR).chunks
        seen = {c.service_id for c in chunks}
        chunks += [c for c in hybrid.search_with_info(stripped, nationwide, k=DISTRICT_CANDIDATES).chunks
                   if c.service_id not in seen]
    except Exception as e:  # 전국 후보를 못 구해도 지역 질문 결과로 답한다
        log.warning("전국 제도 후보 검색 실패: %s", e)
        return []
    return [c for c in _drop_expired(chunks, today) if _agency_region(c.metadata.get("agency") or "", c.service_name) is None]


def _mix_national(chunks: list[Chunk], national: list[Chunk]) -> list[Chunk]:
    """지역 질문의 후보(그 지역 제도 + 전국 제도)에 전국 후보를 섞어, 답변 5자리 중 지역 제도는 MAX_LOCAL_SERVICES개까지,
    전국 제도는 MIN_NATIONAL_SERVICES개 이상이 되게 순서를 정한다. 한쪽이 모자라면 다른 쪽으로 채운다.
    지역 제도는 지역 질문의 검색 순위를, 전국 제도는 지역 낱말을 뺀 검색 순위를 먼저 따른다 (지역 질문 순위에서는 질문과
    더 맞는 전국 제도가 뒤에 있었다: 대전 출산 질문에서 행복출산·산모 건강관리보다 의료급여가 먼저)."""
    order: list[str] = []
    by_service: dict[str, list[Chunk]] = {}
    for c in chunks + national:
        if c.service_id not in by_service:
            order.append(c.service_id)
        by_service.setdefault(c.service_id, []).append(c)
    is_local = {sid: _agency_region(by_service[sid][0].metadata.get("agency") or "", by_service[sid][0].service_name) is not None
                for sid in order}
    local = [s for s in order if is_local[s]]
    nat = list(dict.fromkeys([c.service_id for c in national] + [s for s in order if not is_local[s]]))
    slots = config.MAX_RECOMMENDATIONS
    n_local = min(len(local), MAX_LOCAL_SERVICES, max(slots - min(len(nat), MIN_NATIONAL_SERVICES), 0))
    n_nat = min(len(nat), slots - n_local)
    n_local = min(len(local), slots - n_nat)           # 전국 제도가 모자라면 지역 제도로 채운다
    picked = set(local[:n_local] + nat[:n_nat])
    first = [s for s in order if s in picked]           # 고른 제도는 원래 순서대로, 나머지는 뒤에 (근거 선별이 앞 5개를 쓴다)
    return [c for s in first + [s for s in order if s not in picked] for c in by_service[s]]


def _drop_expired(chunks: list[Chunk], today: date) -> list[Chunk]:
    """신청기한 원문으로 보아 마감이 확실히 지난 제도를 뺀다 (CLAUDE.md: 신청기한이 지난 제도는 후보에서 제외).

    B의 기한 필터는 A가 날짜로 해석한 deadline만 보는데, '2026년 상반기'·'(1차) 5월 22일~6월 22일 (2차) 8월 12일~9월 9일'·
    '2026. 9. 1.~9. 10.'은 deadline이 비어 지난 제도가 그대로 나왔다 (연도가 적힌 기간 134건 중 상당수).
    학기·해마다 다시 모집하는 제도(_recurring_closed: 국가장학금 등)는 빼지 않고 카드에 '다음 공고 확인'으로 안내한다.
    원문을 읽을 수 없으면 거르지 않는다."""
    cache: dict[str, bool] = {}
    keep = []
    for c in chunks:
        if c.service_id not in cache:
            period = _application_period(c.service_id)
            cache[c.service_id] = _period_expired(period, today) and not _recurring_closed(period, today)
        if not cache[c.service_id]:
            keep.append(c)
    return keep


# 끝이 열린 기간: 날짜가 지났어도 마감으로 보지 않는다. '예산 소진'·'마감 시'는 '~' 바로 뒤에 올 때만 열린 끝이다
# ('2025/1/10~2025/12/12(또는 예산 소진 시까지)'는 끝 날짜가 있는 마감, '2026. 3월 ~ 예산 소진 시까지'는 열린 끝)
_OPEN_PERIOD_RE = re.compile(r"상시|수시|연중|미정|~\s*(?:\(예정\))?\s*$"
                             r"|~\s*(?:예산|한도|지원\s*대상|모집\s*인원|인원)[^~/]*?(?:소진|마감)")
_CLOSED_PERIOD_RE = re.compile(r"사업\s*종료|모집\s*종료|미\s*모집")
_PERIOD_TOKEN_RE = re.compile(
    r"(?P<y>20\d\d|\d\d(?=\s*년))\s*(?:년|[.\-/])\s*(?P<m>\d{1,2})(?:\s*(?:월|[.\-/])\s*(?P<d>\d{1,2})(?!\d))?"   # 2026.5.22 / 2026년 5월 / 26년 5월
    r"|(?P<y8>20\d\d)(?P<m8>\d\d)(?P<d8>\d\d)"                                                              # 20260201
    r"|(?P<yh>20\d\d|\d\d(?=\s*년))\s*년?\s*(?P<half>상반기|하반기)"                                          # 2026년 상반기 (소제목 '(하반기)'는 아님)
    r"|(?P<yo>20\d\d|\d\d(?=\s*년))\s*년"                                                                    # 2026년
    r"|(?P<md_m>\d{1,2})\s*(?:월\s*|\.\s*)(?P<md_d>\d{1,2})\s*(?:일|\.)"                                     # 6월 22일 / 6.19.
    r"|(?P<mo>\d{1,2})\s*월"                                                                                 # 12월
    r"|(?<=~)\s*(?P<mo2>\d{1,2})\s*\.(?!\s*\d)")                                                            # '~ 12.' (월만)


def _period_end(text: str) -> date | None:
    """신청기한 원문에서 가장 늦은 날짜 (연도를 알 수 있는 날짜만). 연도 없는 날짜는 앞에 나온 연도를 이어 쓴다
    ('2026. 6. 8.~6.19.'). 일이 없으면 그 달 말일, '2026년 상반기'는 6월 30일·'하반기'는 12월 31일. 연도가 하나도 없으면 None
    ('8월말~9월초'처럼 해마다 반복될 수 있는 기간)."""
    dates = _period_dates(text)
    return max(end for _, end in dates) if dates else None


def _period_start(text: str) -> date | None:
    """신청기한 원문에서 가장 이른 날짜 (일이 없으면 그 달 1일, 상·하반기는 1월·7월 1일). 연도가 없으면 None."""
    dates = _period_dates(text)
    return min(start for start, _ in dates) if dates else None


def _period_dates(text: str) -> list[tuple[date, date]]:
    """신청기한 원문의 날짜들 -> [(그 날짜가 가리키는 첫날, 마지막 날)]. '2026. 5'는 (5/1, 5/31), '2026.5.22'는 (5/22, 5/22)."""
    year: int | None = None
    ends: list[tuple[date, date]] = []

    def add(y: int | None, m: int, d: int | None = None, first_month: int | None = None) -> None:
        if y is None or not 1 <= m <= 12:
            return
        last = calendar.monthrange(y, m)[1]
        if d and d >= 1:
            ends.append((date(y, m, min(d, last)), date(y, m, min(d, last))))
        else:
            ends.append((date(y, first_month or m, 1), date(y, m, last)))

    for t in _PERIOD_TOKEN_RE.finditer(text):
        g = t.groupdict()
        if g["y"]:
            year = int(g["y"]) + (2000 if len(g["y"]) == 2 else 0)
            add(year, int(g["m"]), int(g["d"]) if g["d"] else None)
        elif g["y8"]:
            year = int(g["y8"])
            add(year, int(g["m8"]), int(g["d8"]))
        elif g["half"]:
            year = int(g["yh"]) + (2000 if len(g["yh"]) == 2 else 0)
            first, last_month = (1, 6) if g["half"] == "상반기" else (7, 12)
            add(year, last_month, first_month=first)
        elif g["yo"]:
            year = int(g["yo"]) + (2000 if len(g["yo"]) == 2 else 0)
        elif g["md_m"]:
            add(year, int(g["md_m"]), int(g["md_d"]))
        elif g["mo"] or g["mo2"]:
            add(year, int(g["mo"] or g["mo2"]))
    return ends


# 해마다·학기마다 다시 모집하는 제도의 표현 ('(2026년 2학기) (1차)…', '연 1회', '(매년 상이함)', '(연도별 상이)')
_RECURRING_PERIOD_RE = re.compile(r"학기|매\s*년|연\s*\d\s*회|연도\s*별|분기|정기\s*모집|\(\s*\d\s*차\s*\)|상[.·/]\s*하반기")


def _recurring_closed(text: str, today: date) -> bool:
    """이번 신청은 끝났지만 다시 모집하는 제도인가 (국가장학금처럼 학기·해마다 반복). 빼지 않고 '다음 공고 확인'으로 안내한다."""
    text = " ".join((text or "").split())
    return (_period_expired(text, today) and bool(_RECURRING_PERIOD_RE.search(text))) or _annual_closed(text, today)


# 연도 없이 해마다 같은 달에 받는 기간 ('매년 3월~5월', '연1회 신청(8월말~9월초)'). 활성 제도 471건이 이런 꼴이라
# 연도가 없다는 이유로 올해 기간이 끝난 제도가 표시 없이 안내됐다 (검수: q08 '연1회 신청(8월말~9월초)' 장학금 bad·기한 지남)
_ANNUAL_RE = re.compile(r"매\s*년|해\s*마다|연\s*1\s*회")
_ANNUAL_OPEN_RE = re.compile(r"이후|별도|전\s*년|소진|예산")
_MONTH_RE = re.compile(r"(?<!\d)(\d{1,2})\s*월")


def _annual_closed(text: str, today: date) -> bool:
    """연도 없는 해마다 기간에서 가장 늦은 달이 이번 달보다 앞인가 (올해 신청은 끝나고 다음 해에 다시 받는다).
    연도가 적혀 있거나 끝이 열린 기간('6월 이후'·'재원소진 시까지')·전년도 공고는 판단하지 않는다."""
    if (not _ANNUAL_RE.search(text) or re.search(r"20\d\d", text)
            or _OPEN_PERIOD_RE.search(text) or _ANNUAL_OPEN_RE.search(text)):
        return False
    months = [int(m) for m in _MONTH_RE.findall(text) if 1 <= int(m) <= 12]
    months += [12 if h == "하" else 6 for h in re.findall(r"(상|하)\s*반기", text)]      # 상반기 6월, 하반기 12월
    months += [3 * int(q) for q in re.findall(r"([1-4])\s*분기", text)]
    return bool(months) and max(months) < today.month


def _period_expired(text: str, today: date) -> bool:
    """신청기한 원문으로 보아 마감이 확실히 지났는가. 끝이 열린 기간·연도 없는 기간·빈 값은 False (거르지 않는다)."""
    text = " ".join((text or "").split())
    if not text or _OPEN_PERIOD_RE.search(text):
        return False
    if _CLOSED_PERIOD_RE.search(text):
        return True
    end = _period_end(text)
    return end is not None and end < today


def _drop_other_regions(chunks: list[Chunk], region: str | None) -> tuple[list[Chunk], bool]:
    """사용자가 말한 시·도와 다른 지역의 지자체 제도를 뺀다. (남은 조각, 지역 조건을 풀었는가)

    수집 데이터에 지역 메타데이터가 없어 B의 지역 필터가 아무것도 거르지 못하고, 원문에 '충주시'만 있으면
    시·도 비교도 안 돼 '대구 사는 24살' 질문에 충주시 면접비 지원이 나왔다. 소관기관이 지자체('충청북도 충주시')면
    그 시·도를 제도의 지역으로 본다. 전국 제도(중앙부처·공공기관)는 남긴다.
    다 빠지면 원래 후보를 그대로 쓰고 지역 조건을 풀었다고 알린다 (소프트 필터, CLAUDE.md)."""
    if not region:
        return chunks, False
    region = normalize_region(region) or region
    def supported_region(chunk):
        if chunk.metadata.get("region_provenance") == "explicit_eligibility":
            return normalize_region(chunk.metadata.get("region"))
        return _agency_region(chunk.metadata.get("agency") or "", chunk.service_name)
    keep = [c for c in chunks if supported_region(c) in (None, "전국", region)]
    return (keep, False) if keep else (chunks, True)


# 시·군·구 이름 -> 속한 시·도 짧은 이름 (구 단위 지역 매칭). 보조금24 수집 데이터(services.db, 2026-10-22)의
# 소관기관 이름('서울특별시 강남구')에서 만들었다. 제주시·서귀포시는 소관기관에 없어(제주특별자치도로만 나옴) 직접 넣었다.
# '서구'처럼 여러 시·도에 있는 이름은 시·도가 여럿이다.
DISTRICTS: dict[str, tuple[str, ...]] = {
    '가평군': ('경기',),
    '강남구': ('서울',),
    '강동구': ('서울',),
    '강릉시': ('강원',),
    '강북구': ('서울',),
    '강서구': ('부산', '서울'),
    '강진군': ('전남',),
    '강화군': ('인천',),
    '거제시': ('경남',),
    '거창군': ('경남',),
    '검단구': ('인천',),
    '경산시': ('경북',),
    '경주시': ('경북',),
    '계룡시': ('충남',),
    '계양구': ('인천',),
    '고령군': ('경북',),
    '고성군': ('강원', '경남'),
    '고양시': ('경기',),
    '고창군': ('전북',),
    '고흥군': ('전남',),
    '곡성군': ('전남',),
    '공주시': ('충남',),
    '과천시': ('경기',),
    '관악구': ('서울',),
    '광명시': ('경기',),
    '광산구': ('전남',),
    '광양시': ('전남',),
    '광주시': ('경기',),
    '광진구': ('서울',),
    '괴산군': ('충북',),
    '구례군': ('전남',),
    '구로구': ('서울',),
    '구리시': ('경기',),
    '구미시': ('경북',),
    '군산시': ('전북',),
    '군위군': ('대구',),
    '군포시': ('경기',),
    '금산군': ('충남',),
    '금정구': ('부산',),
    '금천구': ('서울',),
    '기장군': ('부산',),
    '김제시': ('전북',),
    '김천시': ('경북',),
    '김포시': ('경기',),
    '김해시': ('경남',),
    '나주시': ('전남',),
    '남구': ('대구', '부산', '울산', '전남'),
    '남동구': ('인천',),
    '남양주시': ('경기',),
    '남원시': ('전북',),
    '남해군': ('경남',),
    '노원구': ('서울',),
    '논산시': ('충남',),
    '단양군': ('충북',),
    '달서구': ('대구',),
    '달성군': ('대구',),
    '담양군': ('전남',),
    '당진시': ('충남',),
    '대덕구': ('대전',),
    '도봉구': ('서울',),
    '동구': ('대구', '대전', '부산', '울산', '전남'),
    '동대문구': ('서울',),
    '동두천시': ('경기',),
    '동래구': ('부산',),
    '동작구': ('서울',),
    '동해시': ('강원',),
    '마포구': ('서울',),
    '목포시': ('전남',),
    '무안군': ('전남',),
    '무주군': ('전북',),
    '문경시': ('경북',),
    '미추홀구': ('인천',),
    '밀양시': ('경남',),
    '보령시': ('충남',),
    '보성군': ('전남',),
    '보은군': ('충북',),
    '봉화군': ('경북',),
    '부산진구': ('부산',),
    '부안군': ('전북',),
    '부여군': ('충남',),
    '부천시': ('경기',),
    '부평구': ('인천',),
    '북구': ('대구', '부산', '울산', '전남'),
    '사상구': ('부산',),
    '사천시': ('경남',),
    '사하구': ('부산',),
    '산청군': ('경남',),
    '삼척시': ('강원',),
    '상주시': ('경북',),
    '서구': ('대구', '대전', '부산', '전남'),
    '서귀포시': ('제주',),
    '서대문구': ('서울',),
    '서산시': ('충남',),
    '서천군': ('충남',),
    '서초구': ('서울',),
    '서해구': ('인천',),
    '성남시': ('경기',),
    '성동구': ('서울',),
    '성북구': ('서울',),
    '성주군': ('경북',),
    '속초시': ('강원',),
    '송파구': ('서울',),
    '수성구': ('대구',),
    '수영구': ('부산',),
    '수원시': ('경기',),
    '순창군': ('전북',),
    '순천시': ('전남',),
    '시흥시': ('경기',),
    '신안군': ('전남',),
    '아산시': ('충남',),
    '안동시': ('경북',),
    '안산시': ('경기',),
    '안성시': ('경기',),
    '안양시': ('경기',),
    '양구군': ('강원',),
    '양산시': ('경남',),
    '양양군': ('강원',),
    '양주시': ('경기',),
    '양천구': ('서울',),
    '양평군': ('경기',),
    '여수시': ('전남',),
    '여주시': ('경기',),
    '연수구': ('인천',),
    '연제구': ('부산',),
    '연천군': ('경기',),
    '영광군': ('전남',),
    '영덕군': ('경북',),
    '영도구': ('부산',),
    '영동군': ('충북',),
    '영등포구': ('서울',),
    '영암군': ('전남',),
    '영양군': ('경북',),
    '영월군': ('강원',),
    '영종구': ('인천',),
    '영주시': ('경북',),
    '영천시': ('경북',),
    '예산군': ('충남',),
    '예천군': ('경북',),
    '오산시': ('경기',),
    '옥천군': ('충북',),
    '옹진군': ('인천',),
    '완도군': ('전남',),
    '완주군': ('전북',),
    '용산구': ('서울',),
    '용인시': ('경기',),
    '울릉군': ('경북',),
    '울주군': ('울산',),
    '울진군': ('경북',),
    '원주시': ('강원',),
    '유성구': ('대전',),
    '은평구': ('서울',),
    '음성군': ('충북',),
    '의령군': ('경남',),
    '의성군': ('경북',),
    '의왕시': ('경기',),
    '의정부시': ('경기',),
    '이천시': ('경기',),
    '익산시': ('전북',),
    '인제군': ('강원',),
    '임실군': ('전북',),
    '장성군': ('전남',),
    '장수군': ('전북',),
    '장흥군': ('전남',),
    '전주시': ('전북',),
    '정선군': ('강원',),
    '정읍시': ('전북',),
    '제물포구': ('인천',),
    '제주시': ('제주',),
    '제천시': ('충북',),
    '종로구': ('서울',),
    '중구': ('대구', '대전', '부산', '서울', '울산'),
    '중랑구': ('서울',),
    '증평군': ('충북',),
    '진도군': ('전남',),
    '진안군': ('전북',),
    '진주시': ('경남',),
    '진천군': ('충북',),
    '창녕군': ('경남',),
    '창원시': ('경남',),
    '천안시': ('충남',),
    '철원군': ('강원',),
    '청도군': ('경북',),
    '청송군': ('경북',),
    '청양군': ('충남',),
    '청주시': ('충북',),
    '춘천시': ('강원',),
    '충주시': ('충북',),
    '칠곡군': ('경북',),
    '태백시': ('강원',),
    '태안군': ('충남',),
    '통영시': ('경남',),
    '파주시': ('경기',),
    '평창군': ('강원',),
    '평택시': ('경기',),
    '포천시': ('경기',),
    '포항시': ('경북',),
    '하남시': ('경기',),
    '하동군': ('경남',),
    '함안군': ('경남',),
    '함양군': ('경남',),
    '함평군': ('전남',),
    '합천군': ('경남',),
    '해남군': ('전남',),
    '해운대구': ('부산',),
    '홍성군': ('충남',),
    '홍천군': ('강원',),
    '화성시': ('경기',),
    '화순군': ('전남',),
    '화천군': ('강원',),
    '횡성군': ('강원',),
}


def _find_districts(question: str, region: str | None = None) -> set[str]:
    """질문에서 말한 시·군·구 ('강남구에 사는' -> {'강남구'}). 실제 소관기관에 나오는 이름(DISTRICTS)만 인정한다
    ('가구'·'친구'·'수시'처럼 구·시로 끝나는 일반 낱말 차단). 시·도를 말했으면 그 시·도에 있는 이름만
    ('광주시'는 경기도의 시라서 '광주에 사는 … 광주시'처럼 광주광역시를 뜻하면 쓰지 않는다)."""
    found = set()
    for word in re.findall(r"[가-힣]+", question or ""):
        for name in (word, _JOSA_RE.sub("", word)):
            if name in DISTRICTS and (not region or region in DISTRICTS[name]):
                found.add(name)
        # '수원에 살고 있어'처럼 '시'·'군'을 뗀 이름도 시·군으로 본다 (수원 -> 수원시). 안 그러면 다른 시 제도(시흥·화성)가
        # 수원 사는 사람에게 안내됐다. 시·도 이름과 같은 말('광주')과 시·도를 말했는데 거기 없는 이름은 쓰지 않는다.
        bare = _JOSA_RE.sub("", word)
        if len(bare) >= 2 and bare not in condition_extractor.REGIONS:
            for suffix in ("시", "군"):
                full = bare + suffix
                if full in DISTRICTS and (not region or region in DISTRICTS[full]):
                    found.add(full)
    return found


def _agency_districts(agency: str, service_name: str = "") -> set[str]:
    """소관기관의 시·군·구 ('서울특별시 동대문구' -> {'동대문구'}). 시·도 기관('서울특별시')·전국 기관은 빈 집합.
    붙여 쓴 산하 기관 이름은 앞부분의 시·군·구로 본다 ('용산구시설관리공단'·'서울특별시중구시설관리공단'): 띄어 쓴 낱말만
    보다가 '서울 중구 다자녀' 질문에 용산구·송파구 시설관리공단 할인이 후보 10개 중 4개였다 (62곳·325개 제도).
    같은 이름이 여러 시·도에 있는 구(울산 중구)는 시·도 필터(_drop_other_regions)가 먼저 거른다.
    '시'를 뗀 시·군 이름 + 기관 이름('시흥도시공사', 제도 이름의 '(전주보훈요양원)')은 _short_place로 읽는다."""
    parts = (agency or "").split()
    found = {p for p in parts[1:] if p in DISTRICTS}
    for p in parts:
        rest = _REGION_PREFIX_RE.sub("", p)
        name = next((d for d in _DISTRICTS_LONGEST_FIRST if rest.startswith(d) and len(rest) > len(d)), None)
        if name:
            found.add(name)
    found |= {d for _, d in _short_places(agency, service_name) if d}
    return found


_REGION_PREFIX_RE = re.compile("^(?:" + "|".join(sorted(condition_extractor._REGION_ALIASES, key=len, reverse=True)) + ")")
# 지자체 산하 기관·시설 이름의 끝 ('시흥도시공사', '천안복지재단', '전주보훈요양원')
_ORG_SUFFIX_RE = re.compile(r"(?:도시공사|도시관리공사|도시관리공단|시설관리공단|시설공단|환경공단|문화재단|복지재단|체육회|"
                            r"청소년재단|교육재단|장학재단|인재육성재단|보훈요양원|요양원|복지관|청소년수련관)$")
_METRO_SHORT = ("부산", "대구", "인천", "광주", "대전", "울산", "세종")


def _short_place(token: str) -> tuple[str, str | None] | None:
    """'시'·'군'을 뗀 이름 + 기관 이름인 낱말의 (시·도, 시·군). '광주보훈요양원'처럼 광역시 이름이면 (광역시, None).
    전국 기관(한국보훈복지의료공단)이 운영하는 지역 시설이 전국 제도로 보여 '서울 마포구 65세' 질문에 전주보훈요양원,
    '시흥도시공사' 체육시설 감면이 나왔다 (44곳·160개 제도). 끝 글자를 뗀 구 이름('용산복지재단')은 '중'·'남'처럼
    짧아 다른 말과 겹칠 수 있어 보지 않는다. 기관 이름이 뒤에 붙을 때만 읽어 '한국남동발전' 같은 오인을 막는다."""
    t = _REGION_PREFIX_RE.sub("", re.sub(r"^(?:재단법인|사단법인|\(재\)|\(사\))", "", token))
    m = _ORG_SUFFIX_RE.search(t)
    if not m or len(t[:m.start()]) < 2:
        return None
    short = t[:m.start()]
    if short in _METRO_SHORT:
        return short, None
    name = next((short + s for s in "시군" if len(DISTRICTS.get(short + s, ())) == 1), None)
    return (DISTRICTS[name][0], name) if name else None


def _short_places(agency: str, service_name: str = "") -> list[tuple[str, str | None]]:
    """소관기관 낱말과 제도 이름 괄호 안 시설 이름('입소 이용서비스(전주보훈요양원)')에서 읽은 지역들."""
    tokens = (agency or "").split() + ["".join(p.split()) for p in re.findall(r"\(([^)]+)\)", service_name or "")]
    return [p for p in map(_short_place, tokens) if p]


def _drop_other_districts(chunks: list[Chunk], districts: set[str], field: str | None = None,
                          asked: list[str] | None = None) -> list[Chunk]:
    """사용자가 말한 시·군·구와 다른 시·군·구의 제도를 뺀다. 시·도 전체 제도와 전국 제도는 남긴다.

    시·도까지만 비교하면 '서울 강남구에 사는' 질문에 동대문구 주민 대상 제도가 나왔다 (구 단위 제도 208곳).
    그 구의 제도도 관심 분야(field)가 있으면 분야가 맞는 것만 남긴다: 검색어의 '강남구'가 주제와 상관없이 강남구
    제도를 끌어올려 취업준비생 질문에 출산양육지원금·영유아 발달검사가 후보가 됐다 (넓은 질문은 '원하는 도움'으로
    거르지 않아 그대로 안내되기도 함). 시·도 전체·전국 제도는 원래 관련도로 뽑혀서 분야로 거르지 않는다.
    분야가 달라도 찾는 도움(asked)의 핵심 낱말이 제도 원문에 있으면 남긴다: 분야 값이 '생활안정'인 동대문구
    '저소득층 중개보수 지원'(주택임대차 중개보수, 월세 환산보증금)이 '월세 지원' 질문에서 빠졌다.
    다 빠지면 원래 후보를 그대로 쓴다 (소프트 필터)."""
    if not districts:
        return chunks
    keep = []
    for c in chunks:
        own = _agency_districts(c.metadata.get("agency") or "", c.service_name)
        if own and not own & districts:
            continue                       # 다른 구의 제도
        if (own and field and not retrieve_filters.field_matches(c.metadata.get("field"), field)
                and not _service_mentions(c, asked)):
            continue                       # 그 구의 제도지만 관심 분야도, 찾는 도움도 다름
        keep.append(c)
    return keep or chunks


def _service_mentions(c: Chunk, asked: list[str] | None) -> bool:
    """제도 원문 전체(B의 같은 서비스 조각들)에 찾는 도움의 핵심 낱말이 있는가. 조각을 못 가져오면 이 조각만 본다."""
    if not asked:
        return False
    try:
        texts = [x.text for x in evidence_mod.vector_store.get_service_chunks(c.service_id)] or [c.text]
    except Exception:  # 색인 없음 등
        texts = [c.text]
    body = re.sub(r"[^0-9A-Za-z가-힣]", "", " ".join(texts))
    return any(_asked_in(a, body) for a in asked)


def _agency_region(agency: str, service_name: str = "") -> str | None:
    """지자체 소관기관의 시·도 짧은 이름 ('충청북도 충주시' -> '충북'). 전국 기관이면 None.

    지자체 산하 기관은 이름에 시·도가 없다 ('재단법인의왕시청소년재단', '고양시청소년재단'): 지역을 몰라 전국 제도로
    보고 '서울 심리 상담' 질문에 의왕시·시흥시 청소년 상담이 1·2번 카드로 나왔다 (평가 q36). 그래서 이름에 시·군·구
    이름이 그대로('의왕시') 들어 있고 그 이름이 한 시·도에만 있으면 그 시·도로 본다 (_district_region)."""
    if evidence_mod._LOCAL_AGENCY_RE.search(agency):
        found = condition_extractor._find_region(agency)
        if found:
            return found
    regions = {r for r, _ in _short_places(agency, service_name)}
    return _district_region(agency) or (regions.pop() if len(regions) == 1 else None)


def _district_region(agency: str) -> str | None:
    """기관 이름 안의 시·군·구 이름('의왕시', '동대문구')으로 시·도를 찾는다. 같은 이름이 여러 시·도에 있으면
    (중구·서구·고성군) 정할 수 없어 None. 끝 글자를 뗀 이름('의왕')은 보지 않는다: '한국남동발전'이 인천 남동구가 된다."""
    name = "".join((agency or "").split())
    regions = {DISTRICTS[d][0] for d in _UNIQUE_DISTRICTS if d in name}
    return regions.pop() if len(regions) == 1 else None


_UNIQUE_DISTRICTS = sorted((d for d, rs in DISTRICTS.items() if len(rs) == 1), key=len, reverse=True)
_DISTRICTS_LONGEST_FIRST = sorted(DISTRICTS, key=len, reverse=True)


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
