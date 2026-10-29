"""W2 확장: 제도 하나가 '어떤 조건이면 무엇을 받는지'를 조건별(유형·구간)로 정리하고, 사용자가 체크한 조건으로 해당 지원을 가려 준다.

- 정리(`compute`)는 LLM 한 번이고 제도·체크리스트마다 한 번만 한다 (결과를 기억해 둔다). 조건별 해당 여부(`evaluate`)는 코드가
  체크 상태로 바로 계산하므로 체크할 때마다 LLM을 부르지 않는다.
- 각 항목은 basis로 출처를 가른다: 'source'(수집한 원문에 있음) / 'outside'(수집 자료 밖 일반 정보). LLM이 'source'라고 해도
  근거 구절이 원문에 글자 그대로 없거나 지원 내용의 숫자가 원문에 없으면 코드가 'outside'로 내린다 (지어낸 금액을 원문 근거처럼 보이지 않게).
- 'outside'는 팀 결정으로 허용했지만 항상 '자료 밖 정보'로 표시하고 담당 기관 확인을 안내한다. 확실하지 않은 내용은 쓰지 않게 프롬프트로 막는다."""
from __future__ import annotations

import json
import hashlib
import logging
import re
import threading
from contextlib import contextmanager

from src.common.schema import Chunk
from src.generate import condition_extractor

log = logging.getLogger(__name__)

SOURCE, OUTSIDE = "source", "outside"
SECTION_ORDER = ("지원대상", "선정기준", "지원내용", "신청방법")
MAX_TIERS, MAX_NOTES, MAX_AMOUNT = 8, 3, 80
MAX_LABEL, MAX_BENEFIT, MAX_NOTE = 40, 300, 200
MIN_QUOTE = 4
CACHE_LIMIT = 256

PROMPT = """너는 정부 지원제도 하나가 '어떤 조건이면 무엇을 받는지'를 조건별로 정리한다. JSON 객체 하나만 출력한다.
입력은 제도 이름, [원문](지원대상·선정기준·지원내용·신청방법), [체크리스트](번호가 붙은 자격 조건)다.

출력 형식:
{"tiers": [{"label": 짧은 이름, "requires": [체크리스트 번호, ...], "benefit": 이 조건에서 받는 지원(금액·비율·횟수·기간 포함, 한두 문장),
            "amount": [원문]에 적힌 지급 금액 한 줄(예: "월 20만원, 최대 12개월"). 원문에 금액이 없으면 "",
            "quote": [원문]에서 글자 그대로 복사한 근거 구절 (없으면 ""), "basis": "source" 또는 "outside"}],
 "notes": [{"text": 참고 한 문장}]}

규칙:
- 지원 유형·구간·우대(일반/청년/저소득, 자녀 수별, 소득 구간별 등)마다 tier 하나. 모든 사람에게 같은 지원이면 tier 하나, requires는 [].
- requires에는 그 tier를 받으려고 충족해야 하는 [체크리스트] 번호만 넣는다. 체크리스트에 없는 조건이 필요하면 label에만 쓴다.
- [원문]에 있는 내용은 basis "source"로 하고 quote에 원문 구절을 복사한다. 금액·비율·숫자는 원문에 있는 값만 쓴다.
- [원문]에 없지만 네가 확실히 아는 일반 정보(지급 방식·시기·최근 개편·신청 팁)는 basis "outside"로 쓴다.
  확실하지 않으면 쓰지 않는다. 금액·비율·날짜를 추측해 지어내지 않는다.
- notes는 tier로 담기 어려운 참고(중복 지원 여부, 지급 방식 등)이고 [원문]에 있거나 확실히 아는 것만, 최대 3개.
- amount는 [원문]에 있는 금액만 쓰고 계산·추측하지 않는다. 금액이 정해져 있지 않거나 원문에 없으면 "".
- tier는 최대 8개. 한국어 존댓말로 짧게 쓴다."""

_cache: dict[tuple, dict] = {}
_preview = threading.local()


@contextmanager
def preview(result):
    """Admin-only simulated preparation; never populates the public LLM cache."""
    previous = getattr(_preview, "result", None)
    _preview.result = result
    try:
        yield
    finally:
        _preview.result = previous


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d+(?:[.,]\d+)*", text or "")}


def _body(chunk: Chunk) -> str:
    prefix = chunk.service_name + "\n"
    return chunk.text[len(prefix):] if chunk.text.startswith(prefix) else chunk.text


def source_text(chunks: list[Chunk]) -> str:
    """제도의 원문을 항목 이름과 함께 모은다 (지원대상 -> 선정기준 -> 지원내용 -> 신청방법)."""
    parts: dict[str, list[str]] = {}
    for c in sorted(chunks, key=lambda c: c.chunk_id):
        parts.setdefault(c.section, []).append(_body(c))
    return "\n\n".join(f"[{s}]\n" + "".join(parts[s]) for s in SECTION_ORDER if s in parts)


def clean(raw: dict, source: str, n_needs: int) -> dict:
    """LLM 출력을 코드로 검증·정리한다 (형식, 번호 범위, 근거 구절·숫자 대조, 길이)."""
    src_flat, src_numbers = _squash(source), _numbers(source)
    tiers = []
    for item in (raw.get("tiers") or [])[:MAX_TIERS]:
        if not isinstance(item, dict):
            continue
        label, benefit = str(item.get("label") or "").strip(), str(item.get("benefit") or "").strip()
        if not label or not benefit:
            continue
        requires = sorted({i for i in (item.get("requires") or []) if isinstance(i, int) and not isinstance(i, bool) and 1 <= i <= n_needs})
        unresolved = bool(item.get("requires")) and not requires
        quote = str(item.get("quote") or "").strip()
        grounded = (item.get("basis") == SOURCE and len(_squash(quote)) >= MIN_QUOTE and _squash(quote) in src_flat
                    and _numbers(f"{label} {benefit}") <= src_numbers)
        amount = str(item.get("amount") or "").strip()[:MAX_AMOUNT]
        amount_ok = bool(amount) and grounded and bool(_numbers(amount)) and _numbers(amount) <= src_numbers
        tiers.append({"label": label[:MAX_LABEL], "requires": requires, "benefit": benefit[:MAX_BENEFIT],
                      "quote": quote if grounded else "", "basis": SOURCE if grounded else OUTSIDE,
                      "amount": amount if amount_ok else "", "amount_basis": SOURCE if amount_ok else "", "evidence": [],
                      **({"unresolved_requirements": True} if unresolved else {})})
    notes = [{"text": str(n.get("text") or "").strip()[:MAX_NOTE], "basis": OUTSIDE} for n in (raw.get("notes") or [])[:MAX_NOTES]
             if isinstance(n, dict) and str(n.get("text") or "").strip()]
    return {"tiers": tiers, "notes": notes}


def compute(service_id: str, service_name: str, needs: list[str], chunks: list[Chunk] | None = None) -> dict | None:
    """{'tiers': [...], 'notes': [...]} 또는 실패하면 None. needs는 카드의 자격 체크리스트(번호는 1부터)."""
    simulated = getattr(_preview, "result", None)
    if simulated is not None:
        return simulated
    try:
        if chunks is None:
            from src.index import vector_store
            chunks = vector_store.get_service_chunks(service_id)
        source = source_text(chunks)
        if not source.strip():
            return None
        key = ("tiers-v2", service_id, tuple(needs), hashlib.sha256(source.encode()).hexdigest())
        if key in _cache:
            return _cache[key]
        user = json.dumps({"제도 이름": service_name, "원문": source,
                           "체크리스트": [f"{i}. {n}" for i, n in enumerate(needs, start=1)]}, ensure_ascii=False)
        result = clean(condition_extractor._chat_json(PROMPT, user), source, len(needs))
    except Exception as e:      # noqa: BLE001 — 저장소·네트워크·JSON 오류. 카드는 기존 모습 그대로 둔다
        log.warning("조건별 지원 정리 실패(%s): %s", service_id, e)
        return None
    if not result["tiers"]:
        return None
    if len(_cache) >= CACHE_LIMIT:
        _cache.pop(next(iter(_cache)))
    _cache[key] = result
    return result


def evaluate(tiers: list[dict], checked: set[int]) -> dict:
    """체크한 조건(번호 집합)으로 각 지원을 가른다.
    base: 따로 충족할 조건이 없는 지원 / met: 필요한 조건을 모두 체크함 / pending: 아직 체크 안 한 조건이 남음 (missing에 번호)."""
    out: dict[str, list[dict]] = {"base": [], "met": [], "pending": []}
    for t in tiers:
        if t.get("unresolved_requirements"):
            out.setdefault("unresolved", []).append(t)
            continue
        need = set(t["requires"])
        if not need:
            out["base"].append(t)
        elif need <= checked:
            out["met"].append(t)
        else:
            out["pending"].append({**t, "missing": sorted(need - checked)})
    return out
