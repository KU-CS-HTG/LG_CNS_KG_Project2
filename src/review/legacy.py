"""기존 eval/questions.jsonl -> 이벤트. 다시 실행해도 같은 client_event_id라 중복되지 않는다."""
from __future__ import annotations

import json
from pathlib import Path

from src.review import schema
from src.review.store import ReviewStore


def import_legacy(store: ReviewStore, questions_path: str | Path, reviewer: str, services: dict[str, dict]) -> dict:
    """튜닝에 이미 쓴 질문이므로 모두 dev로 둔다 (검증용 test는 새 질문에서만 만든다).

    정답은 good으로 옮긴다. 사람이 검수했으면(reviewed 또는 label_source 없음) human, 아니면 claude 1차 판정(provenance로 구분)."""
    added = {"questions": 0, "judgments": 0, "no_answer": 0, "skipped_existing": 0, "unknown_services": 0}
    for line in Path(questions_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        q = json.loads(line)
        fields = {"legacy_id": q["id"], "category": q.get("category", ""), "topic": q.get("topic", ""),
                  "expected_answer": q.get("expected_answer", ""), "split": "dev", "client_event_id": f"legacy:q:{q['id']}"}
        if q.get("filters"):
            fields["gold_filters"] = {k: v for k, v in q["filters"].items() if k in schema.FILTER_KEYS}
        if q.get("follow_up_of"):
            fields["tags"] = [f"follow_up_of:{q['follow_up_of']}"]
        qid = schema.question_id(q["question"])
        store.add_question(reviewer, q["question"], source="team", **fields)
        added["questions"] += 1 if store.last_written else 0
        added["skipped_existing"] += 0 if store.last_written else 1
        by = "claude" if q.get("label_source") == "claude" and not q.get("reviewed") else "human"
        for sid in q.get("relevant_service_ids", []):
            if sid not in services:
                added["unknown_services"] += 1
            store.add_judgment(reviewer, qid, sid, schema.service_version(services.get(sid)), "good", by=by,
                               client_event_id=f"legacy:j:{q['id']}:{sid}")
            added["judgments"] += 1 if store.last_written else 0
        if q.get("expect_no_answer") and not q.get("relevant_service_ids"):
            store.set_status(reviewer, qid, "no_answer", basis="legacy_import", client_event_id=f"legacy:s:{q['id']}")
            added["no_answer"] += 1 if store.last_written else 0
    return added
