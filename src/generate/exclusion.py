"""W1: 사용자가 이미 이용 중이라고 말한 제도를 추천에서 빼기 (docs/WORK_ITEMS.md W1).

`generator._answer_with_evidence`가 검색·지역 거르기 뒤, 근거 선별 앞에서 `exclude_in_use`를 부른다.
빼는 기준은 둘뿐이고, 둘 다 터미널 추적(RAG_TRACE=1)에 이유가 나온다.
1. 같은 제도: 이용 중인 제도의 이름·표기(LLM이 영어·약칭을 한글 정식 명칭으로 풀어 준 것)가 후보 서비스명에 들어 있을 때.
   이름 비교는 코드가 한다 (LLM이 후보를 '같은 제도'라고 판정하게 하면 국민취업지원제도처럼 관련 높은 제도까지 빠졌다).
2. 같은 종류의 도움: 이용 중인 제도와 후보의 핵심 혜택 종류(직업훈련·소득지원 등 고정 목록 중 하나)가 같을 때.
   훈련이 일부로 들어 있어도 핵심이 소득지원·상담이면 종류가 다르다. 핵심 혜택 종류는 LLM이 정하되, 같은지는 코드가 비교한다.
빼고 나면 후보가 하나도 남지 않을 때는 2번에 걸린 제도를 도로 돌려준다 (이름이 같은 제도는 계속 뺀다).
LLM 호출이 실패하거나 후보에 없는 ID·목록에 없는 종류를 말하면 그 부분은 쓰지 않는다."""
from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar

from src.common.schema import Chunk
from src.generate import benefit_tiers, condition_extractor, trace

log = logging.getLogger(__name__)

# 이번 질문에서 사용자가 이용 중이라고 한 제도의 설명 [{'이름', '설명'}]. 이름으로 찾은 제도의 원문을 모아 두며, 답변 직전 적합성 점검이
# '이 제도의 전제(예: 국민내일배움카드 발급)를 또 안내하는 것'까지 중복으로 볼 수 있게 읽는다. 질문마다 다시 정한다.
IN_USE_CONTEXT: ContextVar[list] = ContextVar("in_use_context", default=[])
CONTEXT_CHARS = 700

MIN_NAME_CHARS = 4          # 코드 비교에서 이름으로 인정하는 최소 글자 수 ('청년' 같은 짧은 말이 우연히 맞는 것을 막는다)
SNIPPET_CHARS = 160         # LLM에 보여 주는 제도 설명 길이
MAX_CANDIDATES = 40         # LLM에 보여 주는 후보 제도 수
KINDS = ("직업훈련", "소득·수당 지원", "취업알선·상담", "대출·금융", "주거", "교육·장학", "창업", "돌봄·육아", "의료·건강", "생활·문화", "기타")
GENERIC_KIND = "기타"       # 종류를 정하기 어려운 제도끼리는 같다고 보지 않는다

RESOLVE_PROMPT = f"""너는 사용자가 이미 이용 중인 제도와 후보 제도들의 '핵심 혜택 종류'를 정하는 도구다.
JSON 객체 하나만 출력한다:
{{"in_use": [{{"name": 이용 중인 제도 이름, "aliases": [같은 제도의 한글 정식 명칭·약칭·영문 표기, ...], "kind": 핵심 혜택 종류}}],
  "candidates": [{{"id": 후보 id, "kind": 핵심 혜택 종류, "reason": 그렇게 정한 이유 한 문장}}]}}

- kind는 반드시 다음 중 하나: {", ".join(KINDS)}.
- kind는 그 제도가 사용자에게 주는 핵심 혜택 하나만 본다. 훈련·교육이 일부로 포함되어도 핵심이 소득·수당 지급이나 취업 상담·알선이면
  직업훈련이 아니다. 확실하지 않으면 {GENERIC_KIND}.
- aliases에는 확실히 같은 제도의 다른 이름만 넣는다 (예: 'K-Digital Training' -> 'K-디지털 트레이닝'). 모르면 빈 배열이다.
- 후보 목록의 모든 id를 candidates에 한 번씩 넣는다."""


def _squash(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s or "").lower()


def _by_service(chunks: list[Chunk]) -> dict[str, list[Chunk]]:
    out: dict[str, list[Chunk]] = {}
    for c in chunks:
        out.setdefault(c.service_id, []).append(c)
    return out


def _same_program(names: list[str], services: dict[str, list[Chunk]]) -> dict[str, str]:
    """이름·표기가 후보 서비스명과 같거나 한쪽이 다른 쪽에 들어 있는 제도 -> 맞은 이름 (둘 다 4자 이상일 때)."""
    wanted = [(n, _squash(n)) for n in names]
    wanted = [(raw, sq) for raw, sq in wanted if len(sq) >= MIN_NAME_CHARS]
    found = {}
    for sid, chunks in services.items():
        service = _squash(chunks[0].service_name)
        if len(service) < MIN_NAME_CHARS:
            continue
        for raw, sq in wanted:
            if sq in service or service in sq:
                found[sid] = raw
                break
    return found


def _ask_kinds(names: list[str], services: dict[str, list[Chunk]]) -> dict | None:
    listed = dict(list(services.items())[:MAX_CANDIDATES])
    user = json.dumps({"이용 중인 제도": names,
                       "후보": [{"id": sid, "이름": cs[0].service_name, "설명": cs[0].text[:SNIPPET_CHARS]} for sid, cs in listed.items()]},
                      ensure_ascii=False)
    try:
        out = condition_extractor._chat_json(RESOLVE_PROMPT, user)
    except Exception as e:      # 네트워크·키·JSON 오류
        log.warning("이용 중인 제도 분석 실패, 이름 비교 결과만 사용: %s", e)
        return None
    return out if isinstance(out, dict) else None


def _describe(service_id: str, chunks: list[Chunk]) -> str:
    """이용 중인 제도의 원문 요약 (지원대상·선정기준·지원내용). 저장소를 읽을 수 없으면 검색으로 받은 조각만 쓴다."""
    try:
        from src.index import vector_store
        chunks = vector_store.get_service_chunks(service_id) or chunks
    except Exception:       # noqa: BLE001 — 색인이 없어도 점검은 계속한다
        pass
    return benefit_tiers.source_text(chunks)[:CONTEXT_CHARS]


def _kind(value) -> str:
    return value.strip() if isinstance(value, str) and value.strip() in KINDS else GENERIC_KIND


def exclude_in_use(question: str, conditions: dict, chunks: list[Chunk]) -> list[Chunk]:
    """이용 중인 제도와 같은 제도·같은 종류의 도움인 제도의 조각을 뺀 목록 (나머지 순서는 그대로).
    이용 중인 제도를 말하지 않았으면 입력을 그대로 돌려준다."""
    IN_USE_CONTEXT.set([])
    names = conditions.get("in_use") or []
    if not names or not chunks:
        return chunks
    services = _by_service(chunks)
    analysis = _ask_kinds(names, services)

    aliases: list[str] = []
    in_use_kinds: dict[str, str] = {}
    for item in (analysis or {}).get("in_use") or []:
        if isinstance(item, dict):
            raw = item.get("name") if isinstance(item.get("name"), str) else ""
            aliases += [a for a in item.get("aliases") or [] if isinstance(a, str)]
            in_use_kinds[raw or "?"] = _kind(item.get("kind"))
    same = _same_program([*names, *aliases], services)

    cand_kind: dict[str, tuple[str, str]] = {}
    for item in (analysis or {}).get("candidates") or []:
        if isinstance(item, dict) and str(item.get("id")) in services:
            reason = item.get("reason") if isinstance(item.get("reason"), str) else ""
            cand_kind[str(item["id"])] = (_kind(item.get("kind")), reason)
    used_kinds = {k for k in in_use_kinds.values() if k != GENERIC_KIND}
    similar = {sid: kr for sid, kr in cand_kind.items() if sid not in same and kr[0] in used_kinds}

    if trace.enabled():
        trace.title("이용 중인 제도 분석")
        trace.out(f"  이용 중: {', '.join(names)}  (분석한 이름·종류: {in_use_kinds or '분석 실패'}, 다른 표기: {aliases or '없음'})")
        for sid, cs in services.items():
            kind, reason = cand_kind.get(sid, ("?", ""))
            if sid in same:
                trace.decision(sid, cs[0].service_name, "제외", f"같은 제도: 이름 '{same[sid]}' 일치")
            elif sid in similar:
                trace.decision(sid, cs[0].service_name, "제외?", f"같은 종류 '{kind}': {reason}")
            else:
                trace.decision(sid, cs[0].service_name, "유지", f"종류 '{kind}'" + (f": {reason}" if reason else ""))

    IN_USE_CONTEXT.set([{"이름": services[sid][0].service_name, "설명": _describe(sid, services[sid])} for sid in same])
    kept = [c for c in chunks if c.service_id not in same and c.service_id not in similar]
    if not kept and similar:       # 같은 종류라서 뺐더니 보여 줄 것이 없으면 도로 돌려준다 (이름이 같은 제도는 계속 뺀다)
        trace.out("  남은 제도가 없어 같은 종류로 뺀 제도를 도로 돌려줍니다")
        kept = [c for c in chunks if c.service_id not in same]
    return kept
