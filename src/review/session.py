"""검수 화면(app/review_mode.py)이 쓰는 로직. 화면 라이브러리 없이 테스트할 수 있게 분리했다.

후보는 질문당 한 번 검색해 순서를 섞어 보여 준다 (1위가 항상 맨 위라는 편향을 줄이고, 어느 검색 방식이 찾았는지는 가린다).
이미 판정한 제도도 함께 보여 주어 고쳐 쓸 수 있게 한다."""
from __future__ import annotations

import random
from datetime import date

from src.review import schema
from src.review.export import _is_expired
from src.review.queue import SearchFn
from src.review.state import State
from src.review.store import ReviewStore

LABEL_TEXT = {"good": "좋음 (이 질문에 맞는 제도)", "partial": "부분 (일부만 맞음)", "bad": "나쁨 (맞지 않음)", "unsure": "보류 (확인 필요)"}
REASON_TEXT = {"not_eligible": "자격이 안 맞음", "wrong_region": "지역이 안 맞음", "expired": "기한이 지남", "irrelevant": "질문과 무관",
               "duplicate": "중복", "generic_noise": "너무 일반적인 제도", "info_wrong": "제도 정보가 틀림", "other": "기타 (메모 필수)",
               "needs_source_check": "원문 확인 필요", "needs_discussion": "논의 필요", "ambiguous_question": "질문이 모호함"}
MEMO_TEXT = {"omission": "빠진 내용", "error": "틀린 내용", "style": "표현·형식", "rule": "앞으로 지킬 규칙"}


def reasons_for(label: str) -> tuple[str, ...]:
    """라벨마다 고를 수 있는 사유. good은 사유가 없다."""
    return {"good": (), "partial": schema.REASONS, "bad": schema.REASONS, "unsure": schema.UNSURE_REASONS}[label]


def questions_overview(state: State, reviewer: str, split: str | None = None) -> list[dict]:
    """검수할 질문 목록과 내가 판정한 제도 수."""
    mine: dict[str, int] = {}
    for (qid, _sid), by_reviewer in state.judgments.items():
        if reviewer in by_reviewer:
            mine[qid] = mine.get(qid, 0) + 1
    rows = [{"question_id": qid, "id": q.get("legacy_id") or qid, "text": q["text"], "split": q["split"], "status": q["status"],
             "mine": mine.get(qid, 0)}
            for qid, q in state.questions.items() if q["status"] in ("active", "no_answer") and (split in (None, "all") or q["split"] == split)]
    return sorted(rows, key=lambda r: (r["mine"] > 0, r["id"]))        # 아직 안 한 질문이 앞


def candidates_for(state: State, qid: str, search_fn: SearchFn, services: dict[str, dict], *, today: date | str, reviewer: str,
                   n: int = 10) -> list[dict]:
    """질문의 후보 제도 (순서 섞음). 항목: service_id, service, source, search_rank, mine(내 판정 이벤트 또는 None), consensus."""
    today_d = date.fromisoformat(today) if isinstance(today, str) else today
    q = state.questions[qid]
    best: dict[str, tuple[int, str]] = {}
    for sid, rank, source in search_fn(q["text"], q.get("gold_filters", {}), n):
        if sid not in best or rank < best[sid][0]:
            best[sid] = (rank, source)
    for sid in state.judged_services(qid):                              # 이미 판정한 것은 검색에 안 나와도 보여 준다
        best.setdefault(sid, (n + 1, "independent_search"))
    out = []
    for sid, (rank, source) in best.items():
        service = services.get(sid)
        if service is None or _is_expired(service, today_d):
            continue
        out.append({"service_id": sid, "service": service, "source": source, "search_rank": rank,
                    "mine": state.judgments.get((qid, sid), {}).get(reviewer),
                    "consensus": state.consensus(qid, sid, schema.service_version(service))})
    out.sort(key=lambda c: c["service_id"])
    random.Random(qid).shuffle(out)                                     # 질문마다 고정된 섞기 (새로고침해도 같은 순서)
    return out


def start_run(store: ReviewStore, reviewer: str, qid: str, candidates: list[dict], *, system: dict | None = None) -> str:
    """이번에 보여 준 후보를 run 이벤트로 남긴다 (판정이 어떤 검색 결과에서 나왔는지 추적)."""
    shown = [{"service_id": c["service_id"], "rank": c["search_rank"], "source": c["source"]} for c in candidates]
    return store.add_run(reviewer, qid, shown, system=system or {"name": "review_mode", "search": "hybrid_filter+vector+bm25"})


def save_judgment(store: ReviewStore, reviewer: str, qid: str, service: dict, label: str, *, reason: str | None = None,
                  note: str | None = None, run_id: str | None = None) -> dict:
    """판정 한 건 저장. 사유 규칙(good은 없음, other는 메모 필수)은 스키마가 검증하고 어기면 ValidationError."""
    note = (note or "").strip() or None
    return store.add_judgment(reviewer, qid, service["service_id"], schema.service_version(service), label,
                              reason=reason if label != "good" else None, note=note, run_id=run_id)


def save_answer_review(store: ReviewStore, reviewer: str, qid: str, answer_text: str, memos: list[dict], *,
                       system: dict | None = None) -> str:
    """시스템 답변 전문을 run으로 남기고 메모를 붙인다. memos: [{kind, text, severity}]. run_id를 돌려준다."""
    run_id = store.add_run(reviewer, qid, [], system=system or {"name": "review_mode"}, answer={"text": answer_text})
    for m in memos:
        store.add_memo(reviewer, run_id, m["kind"], m["text"].strip(), severity=m.get("severity", "minor"))
    return run_id
