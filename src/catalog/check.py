"""카탈로그에서 고쳤지만 검색 색인에는 아직 반영되지 않은 제도를 찾는다 (답변은 색인의 본문을 쓰므로 반영 전에는 옛 내용으로 나온다)."""
from __future__ import annotations

import re

from src.catalog.store import EDITABLE, LABELS, Overlay


def _flat(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s or "")


def unapplied(evidence: dict, overlay: Overlay) -> list[tuple[str, list[str]]]:
    """[(service_id, 색인 본문에 새 값이 없는 항목 이름들)]. evidence: service_id -> 색인에서 읽은 조각들.
    새 값의 앞 20글자(기호·공백 제외)가 조각 본문에 없으면 아직 반영되지 않은 것으로 본다. 분야·기관처럼 본문에 안 들어가는 항목은 보지 않는다."""
    body_fields = ("service_name", "support_target", "selection_criteria", "support_content", "how_to_apply")
    out = []
    for sid, chunks in evidence.items():
        edits = overlay.manual.get(sid) or overlay.edits.get(sid)
        if not edits:
            continue
        text = _flat(" ".join(c.text for c in chunks))
        stale = [LABELS[k] for k in body_fields if k in edits and _flat(edits[k])[:20] and _flat(edits[k])[:20] not in text]
        if stale:
            out.append((sid, stale))
    return out
