"""지역을 말하지 않은 질문에서, 찾는 주제의 제도가 지자체에만 있으면 지자체 제도를 후보에 넣는다 (코드, LLM 없음).

지역이 없으면 B가 지자체 제도를 전국 제도 뒤로 보낸다 (config.LOCAL_PENALTY, '대학 등록금' 질문에 지자체 장학재단
10여 곳이 국가장학금을 덮던 문제). 그런데 '반려동물 병원비도 지원받을 수 있나요?'는 맞는 제도(취약계층 반려동물
의료비 지원 등)가 DB에 모두 지자체 제도라, 상위 후보가 결핵환자 생활보호비·생계급여 같은 관련 없는 전국 제도로
채워졌다 (감점을 끄면 반려동물 제도가 1위). 그래서 찾는 도움의 핵심어(topical.core_terms)가 판정 후보 앞쪽 어느
제도에도 없을 때만, 그 핵심어로 감점 없이 다시 찾아 핵심어가 본문에 있는 지자체 제도를 몇 개 앞에 넣고 지역을
되묻는다. 맞는 전국 제도가 있는 주제(국가장학금)는 지금처럼 전국 제도가 먼저다.

감점 없는 검색은 B의 mode="hybrid"(필터 없음)를 쓰고 나이·기한은 filters.apply_filters로 같은 규칙을 적용한다
(B 파일은 고치지 않는다). 다른 모듈 함수는 모듈 속성으로 부른다 (테스트에서 가짜로 바꿔 끼운다).
"""
from __future__ import annotations

from src.common.schema import Chunk
from src.generate import topical, trace
from src.retrieve import filters as filter_rules
from src.retrieve import hybrid

COVER_WINDOW = 10             # 이 안의 후보에 핵심어가 있으면 주제가 이미 다뤄진 것으로 본다 (판정 후보 수와 같다)
SEARCH_K = 50                 # 감점 없이 다시 찾는 후보 수
MAX_LOCAL = 3                 # 넣는 지자체 제도 최대 수 (같은 이름의 제도는 하나만: 여러 지역에 같은 사업이 있다)
NOTICE = ("'{terms}' 관련 지원은 지역(지자체)마다 따로 있어요. 아래에는 다른 지역 제도가 섞여 있을 수 있으니, "
          "사시는 지역을 알려 주시면 그 지역 제도로 다시 찾아 드릴게요.")


def missing_terms(chunks: list[Chunk], help_: list[str] | None) -> list[str]:
    """찾는 도움의 핵심어 중 앞쪽 후보(COVER_WINDOW개 제도) 어디에도 없는 것."""
    terms = topical.core_terms(help_)
    if not terms:
        return []
    front, seen = [], set()
    for c in chunks:
        if c.service_id not in seen:
            seen.add(c.service_id)
            front.append(c)
        if len(front) == COVER_WINDOW:
            break
    return [t for t in terms
            if not any(topical.match_score(f"{c.service_name} {c.text}", [t]) >= topical.MATCH_RATIO for c in front)]


def local_candidates(chunks: list[Chunk], help_: list[str] | None, filters: dict) -> tuple[list[Chunk], list[str]]:
    """(앞에 넣을 지자체 제도 조각, 앞쪽 후보에 없던 주제어). 질문에 지역이 있거나 주제가 이미 다뤄졌으면 ([], []).
    주제어는 넣을 제도를 못 찾았어도 돌려준다 (generator가 지자체 제도를 안내했을 때 지역을 되묻는 데 쓴다)."""
    if filters.get("region"):
        return [], []
    terms = missing_terms(chunks, help_)
    if not terms:
        return [], []
    query = " ".join(terms)
    try:
        found = hybrid.search_with_info(query, {}, k=SEARCH_K, mode="hybrid").chunks
    except Exception:  # 다시 찾기에 실패해도 원래 후보로 답한다
        return [], []
    found = filter_rules.apply_filters(found, {k: v for k, v in filters.items() if k in ("age", "today")})
    have = {c.service_id for c in chunks}
    out, names = [], set()
    for c in found:
        if (c.service_id in have or c.service_name in names or not filter_rules.is_local(c)
                or topical.match_score(f"{c.service_name} {c.text}", terms) < topical.MATCH_RATIO):
            continue
        names.add(c.service_name)
        out.append(c)
        if len(out) == MAX_LOCAL:
            break
    if out and trace.enabled():
        trace.listing(f"주제어 '{query}'가 앞쪽 후보에 없어 지역 감점 없이 찾은 지자체 제도", out)
    return out, terms


def notice(terms: list[str], help_: list[str] | None = None) -> str:
    """지역을 되묻는 문구. 주제어가 든 '찾는 도움' 구절을 그대로 쓴다 ('병원비'보다 '반려동물 병원비 지원'이 자연스럽다)."""
    phrases = [p.strip() for p in help_ or [] if any(t in p for t in terms)]
    return NOTICE.format(terms=", ".join(phrases or terms))
