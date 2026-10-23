"""검수 대기열: 질문마다 검색한 상위 후보 중 아직 판정이 없거나(new) 제도 내용이 바뀐(changed) 것. 기한이 지난 제도는 올리지 않는다."""
from __future__ import annotations

from datetime import date
from typing import Callable

from src.review import schema
from src.review.export import _is_expired
from src.review.state import State

SearchFn = Callable[[str, dict, int], list[tuple[str, int, str]]]      # (질문, 필터, n) -> [(service_id, rank, source)]


def build_queue(state: State, search_fn: SearchFn, services: dict[str, dict], *, today: date | str, n: int = 30,
                split: str | None = None) -> dict:
    today_d = date.fromisoformat(today) if isinstance(today, str) else today
    items, skipped = [], {"covered": 0, "expired": 0, "inactive": 0}
    for qid, q in sorted(state.questions.items(), key=lambda kv: (kv[1].get("legacy_id") or kv[0])):
        if q["status"] not in ("active", "no_answer") or (split and q["split"] != split):
            continue
        best: dict[str, tuple[int, str]] = {}
        for sid, rank, source in search_fn(q["text"], q.get("gold_filters", {}), n):
            if sid not in best or rank < best[sid][0]:
                best[sid] = (rank, source)
        for sid, (rank, source) in sorted(best.items(), key=lambda kv: kv[1][0]):
            service = services.get(sid)
            if service is None:
                skipped["inactive"] += 1
                continue
            if _is_expired(service, today_d):
                skipped["expired"] += 1
                continue
            c = state.consensus(qid, sid, schema.service_version(service))
            if c["status"] in ("consensus", "adjudicated", "unsure", "conflict"):
                skipped["covered"] += 1                     # 판정이 있음 (unsure·conflict는 아래 별도 목록)
                continue
            items.append({"question_id": qid, "id": q.get("legacy_id") or qid, "service_id": sid, "kind": "changed" if c["status"] == "stale" else "new",
                          "rank": rank, "source": source, "name": service.get("service_name", "")})
    items.sort(key=lambda i: (i["kind"] != "changed", i["rank"], i["id"]))
    unsure, conflicts = [], []
    for (qid, sid) in sorted(state.judgments):
        q, service = state.questions.get(qid), services.get(sid)
        if not q or service is None or q["status"] not in ("active", "no_answer") or (split and q["split"] != split):
            continue
        if _is_expired(service, today_d):
            continue
        c = state.consensus(qid, sid, schema.service_version(service))
        row = {"question_id": qid, "id": q.get("legacy_id") or qid, "service_id": sid, "name": service.get("service_name", "")}
        if c["status"] == "unsure":
            unsure.append({**row, "reasons": c["reasons"]})
        elif c["status"] == "conflict":
            conflicts.append({**row, "labels": c.get("labels", {})})
    return {"items": items, "unsure": unsure, "conflicts": conflicts, "skipped": skipped}
