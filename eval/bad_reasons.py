"""검색 상위 K개 중 사람이 '나쁨'으로 판정한 제도의 사유별 집계 (WORK_ITEMS R1 1단계).

bad@5(상위 5개 중 나쁨 비율)만으로는 무엇을 고쳐야 할지 모른다. 검수 저장소(reviews/events)의 판정 사유
(src/review/schema.REASONS: wrong_region·irrelevant·not_eligible …)를 상위 K개의 나쁨 제도마다 읽어 센다.
WORK_ITEMS의 '지역 불일치 88건'은 검수한 후보 전체(대부분 지역 감점 LOCAL_PENALTY 이전 검색 결과)를 센 숫자라,
지금 검색의 상위 K개에서 무엇이 남았는지는 이렇게 다시 세어야 안다 (2026-10-28 dev_v2: 지역 불일치 2건, 관련 없음 22건).
지역 불일치는 질문에 지역이 있었는지로 나눈다: 있으면 다른 지역 제도가 섞인 것(지역 필터·거르기 문제), 없으면
지역을 모르는데 지자체 제도가 올라온 것(지역 감점 문제). 고칠 곳이 다르다.

시험지(reviews/exports/*.jsonl)의 hard_negatives가 나쁨 판정 제도이고, 사유는 시험지에 없어 검수 저장소에서 읽는다.
질문 지역은 condition_extractor._find_region(질문 문장, 코드·LLM 없음)으로 본다 (run_eval은 시험지의 filters만 쓰고
dev 시험지에는 filters가 없다).
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable

NO_REASON = "(사유 없음)"
EXAMPLE_LIMIT = 8             # 사유마다 보고서에 보여 줄 사례 수


def summarize(rows: list[dict], k: int, consensus: Callable[[str, str], dict],
              question_region: Callable[[str], str | None], describe: Callable[[str], str] | None = None) -> dict:
    """rows: run_experiment의 questions (question_id·retrieved·hard_negatives 필요).
    consensus(question_id, service_id) -> ReviewStore 합의 결과 (reasons 목록). describe(service_id) -> 사례에 쓸 이름·기관."""
    by_reason: Counter = Counter()
    region_split: Counter = Counter()
    examples: dict[str, list[dict]] = {}
    total = bad_total = 0
    for r in rows:
        if "hard_negatives" not in r:
            continue
        top = r["retrieved"][:k]
        total += len(top)
        bad = [sid for sid in top if sid in set(r["hard_negatives"])]
        q_region = question_region(r["question"]) if bad else None
        for sid in bad:
            bad_total += 1
            reasons = consensus(r.get("question_id") or "", sid).get("reasons") or [NO_REASON]
            for reason in reasons:                       # 검수자마다 사유가 다르면 각각 센다
                by_reason[reason] += 1
                cases = examples.setdefault(reason, [])
                if len(cases) < EXAMPLE_LIMIT:
                    cases.append({"id": r["id"], "question": r["question"], "question_region": q_region,
                                  "service_id": sid, "service": describe(sid) if describe else sid,
                                  "rank": top.index(sid) + 1})
            if "wrong_region" in reasons:
                region_split["question_region" if q_region else "no_region"] += 1
    return {"k": k, "results": total, "bad": bad_total,
            "by_reason": dict(by_reason.most_common()),
            "wrong_region": {"question_region": region_split["question_region"], "no_region": region_split["no_region"]},
            "examples": {reason: examples[reason] for reason in by_reason}}


def format_summary(s: dict) -> str:
    k = s["k"]
    lines = [f"  상위 {k}개 중 나쁨 판정 {s['bad']}건 / 결과 {s['results']}건 — 사유별 (한 제도에 사유가 여럿이면 각각 셈):"]
    for reason, n in s["by_reason"].items():
        lines.append(f"    {reason:<14s} {n:>3d}")
    wr = s["wrong_region"]
    if wr["question_region"] or wr["no_region"]:
        lines.append(f"  지역 불일치 중: 질문에 지역 있음(다른 지역 제도) {wr['question_region']}건 / "
                     f"질문에 지역 없음(지자체 제도가 올라옴) {wr['no_region']}건")
    for reason, cases in s["examples"].items():
        lines.append(f"  사례 — {reason}:")
        for e in cases:
            where = e["question_region"] or "지역 없음"
            lines.append(f"    [{e['id']}] ({where}) {e['question'][:30]} → {e['rank']}위 {e['service']}")
    return "\n".join(lines)


def review_lookups():
    """실제 검수 저장소·조건 추출·색인으로 summarize에 넘길 함수들. 색인이 없으면 describe는 service_id만."""
    from src.generate import condition_extractor
    from src.review.store import ReviewStore

    state = ReviewStore().state()

    def describe(sid: str) -> str:
        try:
            from src.index import vector_store
            chunks = vector_store.get_service_chunks(sid)
        except Exception:
            return sid
        if not chunks:
            return sid
        md = chunks[0].metadata
        return f"{chunks[0].service_name} ({md.get('agency') or '기관 미상'}, 지역 {md.get('region') or '미상'})"

    return state.consensus, condition_extractor._find_region, describe
