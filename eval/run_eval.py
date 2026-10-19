"""[C. 평가] 검색 모드별 Hit Rate@K · Recall@K · MRR · 조건 위반율 비교 (기획서 9.2, 9.4)

사용법:
    python eval/run_eval.py                       # 모든 모드, K=5
    python eval/run_eval.py --modes bm25 hybrid --k 3
    python eval/run_eval.py --verbose             # 질문별 결과까지 출력

LLM 조건 추출을 거치지 않고 questions.jsonl의 user 조건을 그대로 써서 '검색'만 평가한다.
결과는 eval/results.csv 에 누적 저장 → 기획서 9.4 실험 기록 표에 옮겨 적는다.
"""
import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from src.config import EMBEDDING_PROVIDER, SEARCH_MODES  # noqa: E402
from src.regions import normalize_region, normalize_sigungu  # noqa: E402
from src.retriever import search, search_multi  # noqa: E402
from src.schema import Conditions  # noqa: E402

HERE = Path(__file__).parent


def load_cases() -> list[dict]:
    lines = (HERE / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in lines if l.strip()]


def plan_queries(cases: list[dict]) -> dict[str, tuple[str, str | None]]:
    """--rewrite: 질문마다 LLM으로 검색어·분야를 한 번만 만들어 모든 모드에서 재사용."""
    from src.conditions import analyze
    out = {}
    for c in cases:
        p = analyze(c["question"])
        out[c["id"]] = (p.search_query, p.category)
        print(f"  [{c['id']}] {c['question']} → '{p.search_query}' / 분야: {p.category}")
    return out


def evaluate(mode: str, k: int, cases: list[dict], verbose: bool,
             plans: dict[str, tuple[str, str | None]] | None = None) -> dict:
    hit, recall, rr, scored = 0.0, 0.0, 0.0, 0     # scored = 정답이 있는 질문 수
    violations, checked = 0, 0                      # 나오면 안 되는 제도가 나온 질문 수
    for c in cases:
        u = c["user"]
        cond = Conditions(age=u.get("age"), region=normalize_region(u.get("region")),
                          sigungu=normalize_sigungu(u.get("sigungu")))
        if plans:   # 앱과 똑같이: 원문 + 재작성 검색어 Multi Query + 분야 가산점
            query, category = plans[c["id"]]
            hits = search_multi([c["question"], query], cond, mode=mode, k=k, category=category)
        else:       # Baseline: 원문 그대로
            hits = search(c["question"], cond, mode=mode, k=k)
        got = [h.service_id for h in hits]
        expected, forbidden = set(c["expected_ids"]), set(c.get("forbidden_ids", []))

        if expected:
            scored += 1
            found = [sid for sid in got if sid in expected]
            hit += 1.0 if found else 0.0
            recall += len(found) / len(expected)
            first = next((i for i, sid in enumerate(got, 1) if sid in expected), None)
            rr += 1.0 / first if first else 0.0
        if forbidden:
            checked += 1
            violations += bool(forbidden & set(got))
        if verbose:
            mark = "✅" if (not expected or expected & set(got)) and not (forbidden & set(got)) else "❌"
            print(f"  {mark} [{c['id']}] {c['question']} → {got}")

    return {
        "mode": mode, "k": k,
        "hit_rate": hit / scored if scored else 0.0,
        "recall": recall / scored if scored else 0.0,
        "mrr": rr / scored if scored else 0.0,
        "violation_rate": violations / checked if checked else 0.0,
        "n_questions": len(cases),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--modes", nargs="+", default=list(SEARCH_MODES), choices=SEARCH_MODES)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--rewrite", action="store_true",
                        help="LLM 검색어 재작성 + 분야 가산점 적용 (없으면 원문 질문 그대로 = Baseline)")
    args = parser.parse_args()

    cases = load_cases()
    plans = plan_queries(cases) if args.rewrite else None
    rows = []
    for mode in args.modes:
        if args.verbose:
            print(f"\n[{mode}]")
        try:
            r = evaluate(mode, args.k, cases, args.verbose, plans)
            r["rewrite"] = args.rewrite
            rows.append(r)
        except RuntimeError as e:   # 벡터DB 미생성 등
            print(f"[{mode}] 건너뜀: {e}")

    print(f"\n임베딩: {EMBEDDING_PROVIDER} | 재작성: {'ON' if args.rewrite else 'OFF'} | "
          f"질문 {len(cases)}개 | K={args.k}")
    print(f"{'mode':<8}{'Hit@K':>8}{'Recall@K':>10}{'MRR':>8}{'조건위반':>10}")
    for r in rows:
        print(f"{r['mode']:<8}{r['hit_rate']:>8.2f}{r['recall']:>10.2f}{r['mrr']:>8.2f}{r['violation_rate']:>10.2f}")

    out = HERE / "results.csv"
    new = not out.exists()
    with out.open("a", newline="", encoding="utf-8-sig") as f:   # utf-8-sig: 엑셀에서 한글 안 깨짐
        w = csv.DictWriter(f, fieldnames=["time", "embedding", *rows[0].keys()] if rows else ["time"])
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "embedding": EMBEDDING_PROVIDER, **r})
    print(f"\n결과 누적 저장 → {out}")


if __name__ == "__main__":
    main()
