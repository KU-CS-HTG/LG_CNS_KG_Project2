"""W1: 사용자가 이미 이용 중이라고 말한 제도를 추천에서 빼기 (docs/WORK_ITEMS.md W1).

`generator._answer_with_evidence`가 검색·지역 거르기 뒤, 근거 선별 앞에서 `exclude_in_use`를 부른다.
- 같은 제도(이름이 같거나 같은 제도의 다른 이름·운영 기관) -> 후보에서 뺀다.
- 같은 성격이라 겹칠 수 있는 제도(이미 듣는 직업훈련과 또 다른 직업훈련) -> 빼지 않고 뒤로 보낸다. 맞는 제도를
  잘못 빼는 위험이 커서 제외하지 않는다 (카드에 '겹칠 수 있음' 표시를 붙이는 것은 카드 쪽 후속 과제).
이름이 영어와 한글로 다른 경우('K-Digital Training'과 'K-디지털 트레이닝')는 코드 비교로 못 잡으므로 LLM에게
후보 목록 안에서 고르게 한다. LLM이 후보에 없는 ID를 말하거나 호출이 실패하면 그 결과는 쓰지 않는다 (코드 검증)."""
from __future__ import annotations

import json
import logging
import re

from src.common.schema import Chunk
from src.generate import condition_extractor

log = logging.getLogger(__name__)

MIN_NAME_CHARS = 4          # 코드 비교에서 이름으로 인정하는 최소 글자 수 ('청년' 같은 짧은 말이 우연히 맞는 것을 막는다)
SNIPPET_CHARS = 120         # LLM에 보여 주는 제도 설명 길이
MAX_CANDIDATES = 40         # LLM에 보여 주는 후보 제도 수

RESOLVE_PROMPT = """너는 사용자가 이미 이용 중인 제도와 후보 제도 목록을 비교하는 도구다.
JSON 객체 하나만 출력한다: {"same": [id, ...], "similar": [id, ...]}

- same: 사용자가 이용 중인 제도와 같은 제도인 후보 (이름 표기가 달라도 같은 제도, 그 제도의 운영 기관·세부 과정 포함).
- similar: 같은 제도는 아니지만 같은 종류의 도움(예: 둘 다 취업 직업훈련)이라 중복해서 받기 어렵거나 겹칠 가능성이 큰 후보.
- 주제가 비슷하다는 이유만으로 similar에 넣지 않는다. 확실하지 않으면 넣지 않는다.
- id는 후보 목록에 있는 것만 쓴다. 해당하는 것이 없으면 빈 배열이다."""


def _squash(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s or "").lower()


def _by_service(chunks: list[Chunk]) -> dict[str, list[Chunk]]:
    out: dict[str, list[Chunk]] = {}
    for c in chunks:
        out.setdefault(c.service_id, []).append(c)
    return out


def _code_same(names: list[str], services: dict[str, list[Chunk]]) -> set[str]:
    """이름이 같거나 한쪽이 다른 쪽에 들어 있는 제도 (둘 다 4자 이상일 때)."""
    wanted = [n for n in (_squash(x) for x in names) if len(n) >= MIN_NAME_CHARS]
    same = set()
    for sid, chunks in services.items():
        service = _squash(chunks[0].service_name)
        if len(service) >= MIN_NAME_CHARS and any(w in service or service in w for w in wanted):
            same.add(sid)
    return same


def _resolve(names: list[str], services: dict[str, list[Chunk]]) -> tuple[set[str], set[str]]:
    """(같은 제도 ID들, 겹칠 수 있는 제도 ID들). 코드 비교 결과에 LLM이 후보 목록에서 고른 것을 더한다."""
    same = _code_same(names, services)
    similar: set[str] = set()
    listed = {sid: chunks for sid, chunks in list(services.items())[:MAX_CANDIDATES]}
    user = json.dumps({"이용 중인 제도": names,
                       "후보": [{"id": sid, "이름": chunks[0].service_name, "설명": chunks[0].text[:SNIPPET_CHARS]}
                              for sid, chunks in listed.items()]}, ensure_ascii=False)
    try:
        out = condition_extractor._chat_json(RESOLVE_PROMPT, user)
    except Exception as e:      # 네트워크·키·JSON 오류. 코드로 찾은 것만 쓴다
        log.warning("이용 중인 제도 판별 실패, 이름 비교 결과만 사용: %s", e)
        return same, similar
    for key, bucket in (("same", same), ("similar", similar)):
        ids = out.get(key)
        if isinstance(ids, list):
            bucket.update(i for i in ids if isinstance(i, str) and i in listed)   # 후보에 없는 ID는 버린다
    return same, similar - same


def exclude_in_use(question: str, conditions: dict, chunks: list[Chunk]) -> list[Chunk]:
    """이미 이용 중인 제도의 조각은 빼고, 겹칠 수 있는 제도의 조각은 뒤로 보낸 목록을 돌려준다 (나머지 순서는 그대로).
    이용 중인 제도를 말하지 않았으면 입력을 그대로 돌려준다."""
    names = conditions.get("in_use") or []
    if not names or not chunks:
        return chunks
    same, similar = _resolve(names, _by_service(chunks))
    kept = [c for c in chunks if c.service_id not in same]
    return [c for c in kept if c.service_id not in similar] + [c for c in kept if c.service_id in similar]
