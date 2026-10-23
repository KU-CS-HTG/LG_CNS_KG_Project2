"""판정 -> 평가용 시험지(eval 형식) 내보내기. 기한이 지난 제도는 판정과 무관하게 자동 제외한다 (판정은 지우지 않는다)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.review import schema
from src.review.state import State

COVERAGE_AT = 10


def _is_expired(service: dict, today: date) -> bool:
    from eval.label_helper import deadline_status          # 기한 문자열 해석 (신청기한이 비면 unknown = 지난 것으로 보지 않음)

    return deadline_status(service.get("application_period") or "", today)[0] == "expired"


def build_rows(state: State, *, split: str, today: date | str, services: dict[str, dict]) -> tuple[list[dict], dict]:
    today_d = date.fromisoformat(today) if isinstance(today, str) else today
    report = {"questions": 0, "with_relevant": 0, "no_answer": 0, "without_relevant": 0, "expired_excluded": 0, "stale": 0,
              "unsure": 0, "missing_services": 0, "conflicts": []}
    rows = []
    for qid, q in sorted(state.questions.items(), key=lambda kv: (kv[1].get("legacy_id") or kv[0])):
        if q["status"] not in ("active", "no_answer") or (split != "all" and q["split"] != split):
            continue
        good, partial, bad, unsure, stale, conflicts, sources = [], [], [], [], [], [], set()
        covered, judged_ok = {}, set()
        for sid in state.judged_services(qid):
            service = services.get(sid)
            if service is None:
                report["missing_services"] += 1
                continue
            if _is_expired(service, today_d):
                report["expired_excluded"] += 1
                continue
            c = state.consensus(qid, sid, schema.service_version(service))
            if c["status"] == "stale":
                stale.append(sid)
            elif c["status"] == "conflict":
                conflicts.append(sid)
            elif c["status"] == "unsure":
                unsure.append(sid)
            elif c["status"] in ("consensus", "adjudicated"):
                judged_ok.add(sid)
                sources.update(c["provenance"])
                {"good": good, "partial": partial, "bad": bad}[c["label"]].append(sid)
        run = state.runs.get(state.latest_run.get(qid, ""))
        coverage = None
        if run:
            shown = [c["service_id"] for c in run["candidates"] if c["rank"] <= COVERAGE_AT and c["service_id"] in services
                     and not _is_expired(services[c["service_id"]], today_d)]
            coverage = round(sum(1 for sid in shown if sid in judged_ok or sid in unsure) / len(shown), 3) if shown else None
        expect_no_answer = q["status"] == "no_answer" and not good
        if q["status"] == "no_answer" and good:
            conflicts.append("(no_answer인데 good 판정이 있음)")
        row = {"id": q.get("legacy_id") or qid, "question_id": qid, "category": q.get("category") or (q.get("tags") or [""])[0],
               "topic": q.get("topic", ""), "question": q["text"], "relevant_service_ids": sorted(good),
               "relevance": {**{s: 2 for s in sorted(good)}, **{s: 1 for s in sorted(partial)}}, "hard_negatives": sorted(bad),
               "expected_answer": q.get("expected_answer", ""), "expect_no_answer": expect_no_answer, "split": q["split"],
               "judged_coverage_at_10": coverage, "unsure": sorted(unsure), "conflicts": conflicts, "stale": sorted(stale),
               "label_sources": sorted(sources)}
        if q.get("gold_filters"):
            row["filters"] = q["gold_filters"]
        rows.append(row)
        report["questions"] += 1
        report["stale"] += len(stale)
        report["unsure"] += len(unsure)
        report["conflicts"] += [f"{row['id']}:{c}" for c in conflicts]
        if good:
            report["with_relevant"] += 1
        elif expect_no_answer:
            report["no_answer"] += 1
        else:
            report["without_relevant"] += 1
    return rows, report


def export_eval(state: State, *, split: str, today: date | str, services: dict[str, dict], path: str | Path, overwrite: bool = False) -> dict:
    """시험지를 path에 쓴다. 이미 있는 파일은 덮어쓰지 않는다 (test 시험지는 한 번 만들면 고정)."""
    path = Path(path)
    if path.exists() and not (overwrite and split != "test"):
        raise FileExistsError("frozen_export_exists" if split == "test" else "export_exists")
    rows, report = build_rows(state, split=split, today=today, services=services)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    report["path"] = str(path)
    return report
