"""검색 평가 실행 (기획서 9.2, 9.4). 질문 세트로 검색을 돌려 Hit Rate·Recall·MRR을 계산하고 결과를 저장한다.

실험 순서 (기획서 6.2, 9.4): Baseline(SQL+키워드) -> Vector -> BM25 -> Hybrid -> Hybrid+정형 필터 -> 최종.
Top-K는 3 / 5 / 10을 한 번에 계산하고, --k는 표에서 강조할 K와 질문마다 가져올 최소 깊이를 정한다.

실행 예
    python -m eval.run_eval --experiment hybrid_filter --k 5          # 한 실험
    python -m eval.run_eval --experiment all                          # 전체 비교표(기획서 9.4에 옮겨 적기)
    python -m eval.run_eval --experiment vector --questions my.jsonl --out-dir /tmp/out --today 2026-10-19

전제: 색인이 있어야 한다 (python -m src.index.vector_store ...). baseline은 retrieve/baseline.py가 구현돼야 한다.

질문 세트(eval/questions.jsonl) 한 줄의 필드
    id, category, question, relevant_service_ids, expected_answer, expect_no_answer,
    filters(선택): 이 질문의 정형 조건. 검색 평가는 LLM 조건 추출과 분리하려고 정답 조건을 직접 적는다
                   예: {"age": 27, "region": "서울"}. hybrid_filter 실험에서만 쓰인다.
    todo(선택): 정답을 아직 못 정한 항목. relevant_service_ids가 비어 있고 expect_no_answer도 아니면 평가에서 제외한다.

질문의 종류
    - 평가 대상     : relevant_service_ids가 있음 -> Hit Rate / Recall / MRR 계산
    - 근거 없음     : expect_no_answer=true -> 검색 지표에서 제외, no_evidence_rate로 따로 본다
    - 정답 미지정   : 위 둘 다 아님 -> 제외하고 몇 개인지 경고한다

답변 평가(Answer Correctness 등)는 C가 eval/answer_eval.py와 연결한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path

from eval import metrics
from src.common import config

QUESTIONS_PATH = config.EVAL_DIR / "questions.jsonl"
RESULTS_DIR = config.EVAL_DIR / "results"
EXPERIMENTS = ("baseline", "vector", "bm25", "hybrid", "hybrid_filter")
FAILURE_LIMIT = 10          # 보고서에 보여 줄 놓친 질문 수

# (질문, 필터, 가져올 개수) -> (service_id 순위 리스트, 실제 적용된 필터, 필터를 풀었는지)
SearchFn = Callable[[str, dict, int], "tuple[list[str], dict, bool]"]


def load_questions(path: Path | str | None = None) -> list[dict]:
    with open(path or QUESTIONS_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def split_questions(questions: Sequence[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(평가 대상, 근거 없음, 정답 미지정)으로 나눈다."""
    scored, no_answer, unlabeled = [], [], []
    for q in questions:
        if q.get("relevant_service_ids"):
            scored.append(q)
        elif q.get("expect_no_answer"):
            no_answer.append(q)
        else:
            unlabeled.append(q)
    return scored, no_answer, unlabeled


# ---------------------------------------------------------------------------
# 실험별 검색 함수
# ---------------------------------------------------------------------------
def make_search_fn(experiment: str, today: str | None = None) -> SearchFn:
    """실험 이름 -> 검색 함수. 검색 모듈은 모듈 속성으로 불러서(테스트에서 바꿔 끼울 수 있게) 쓴다."""
    if experiment not in EXPERIMENTS:
        raise ValueError(f"알 수 없는 실험: {experiment!r} (가능: {EXPERIMENTS})")

    if experiment == "baseline":
        from src.retrieve import baseline

        def search_baseline(query: str, filters: dict, depth: int):
            chunks = baseline.search(query, filters, depth)
            return _service_ids(chunks), {}, False

        return search_baseline

    from src.retrieve import hybrid

    def search_index(query: str, filters: dict, depth: int):
        flt = dict(filters)
        if experiment == "hybrid_filter" and today:
            flt.setdefault("today", today)
        result = hybrid.search_with_info(query, flt, depth, experiment)  # type: ignore[arg-type]
        return _service_ids(result.chunks), result.filters_used, result.relaxed

    return search_index


def _service_ids(chunks) -> list[str]:
    seen: list[str] = []
    for c in chunks:
        if c.service_id not in seen:
            seen.append(c.service_id)
    return seen


# ---------------------------------------------------------------------------
# 실행·집계
# ---------------------------------------------------------------------------
def run_experiment(experiment: str, questions: Sequence[dict], search_fn: SearchFn, k: int = config.DEFAULT_TOP_K,
                   today: str | None = None) -> dict:
    """모든 질문에 검색을 돌려 지표를 계산한다. 반환 dict는 그대로 JSON으로 저장된다."""
    scored, no_answer, unlabeled = split_questions(questions)
    if not scored:
        raise ValueError("정답(relevant_service_ids)이 지정된 질문이 없습니다. eval/questions.jsonl의 정답을 채우세요.")
    ks = sorted(set(metrics.DEFAULT_KS) | {k})
    depth = max(ks)

    def ask(q: dict) -> dict:
        ids, used, relaxed = search_fn(q["question"], q.get("filters", {}), depth)
        relevant = set(q.get("relevant_service_ids", []))
        hit_rank = next((i for i, sid in enumerate(ids, start=1) if sid in relevant), None)
        return {"id": q["id"], "category": q.get("category", ""), "question": q["question"],
                "relevant": sorted(relevant), "retrieved": ids, "first_hit_rank": hit_rank,
                "filters": q.get("filters", {}), "filters_used": used, "relaxed": relaxed}

    rows = [ask(q) for q in scored]
    na_rows = [ask(q) for q in no_answer]

    retrieved = [r["retrieved"] for r in rows]
    relevant = [set(r["relevant"]) for r in rows]
    summary = metrics.evaluate(retrieved, relevant, ks)

    by_category: dict[str, dict[str, float]] = {}
    for cat in sorted({r["category"] for r in rows}):
        sel = [r for r in rows if r["category"] == cat]
        by_category[cat] = {
            "n": len(sel),
            f"hit_rate@{k}": metrics.hit_rate_at_k([r["retrieved"] for r in sel], [set(r["relevant"]) for r in sel], k),
            f"recall@{k}": metrics.recall_at_k([r["retrieved"] for r in sel], [set(r["relevant"]) for r in sel], k),
            "mrr": metrics.mrr([r["retrieved"] for r in sel], [set(r["relevant"]) for r in sel]),
        }

    return {
        "experiment": experiment,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "config": {"k": k, "ks": ks, "today": today or date.today().isoformat(), "embedding_model": config.EMBEDDING_MODEL,
                   "rrf_k": config.RRF_K, "hybrid_candidates": config.HYBRID_CANDIDATES, "hybrid_weights": list(config.HYBRID_WEIGHTS)},
        "counts": {"scored": len(rows), "no_answer": len(na_rows), "unlabeled": len(unlabeled)},
        "metrics": summary,
        "targets": metrics.check_targets(summary),
        "no_evidence_rate": metrics.no_evidence_rate([r["retrieved"] for r in na_rows]) if na_rows else None,
        "by_category": by_category,
        "questions": rows,
        "no_answer_questions": na_rows,
        "unlabeled_ids": [q["id"] for q in unlabeled],
    }


# ---------------------------------------------------------------------------
# 출력·저장
# ---------------------------------------------------------------------------
def format_report(result: dict) -> str:
    cfg, cnt, m = result["config"], result["counts"], result["metrics"]
    k = cfg["k"]
    lines = [f"=== {result['experiment']}  (K={k}, 평가 {cnt['scored']}문항 / 근거 없음 {cnt['no_answer']} / 정답 미지정 {cnt['unlabeled']}) ==="]
    for kk in cfg["ks"]:
        lines.append(f"  K={kk:<2d}  Hit Rate {m[f'hit_rate@{kk}']:.3f}   Recall {m[f'recall@{kk}']:.3f}")
    lines.append(f"  MRR   {m['mrr']:.3f}")
    if result["targets"]:
        goals = ", ".join(f"{name} {'달성' if ok else '미달'}(목표 {metrics.TARGETS[name]:.2f})" for name, ok in result["targets"].items())
        lines.append(f"  잠정 목표(Top-5): {goals}")
    if result["no_evidence_rate"] is not None:
        lines.append(f"  근거 없음 질문에서 빈 결과를 돌려준 비율: {result['no_evidence_rate']:.2f}  (높을수록 좋음)")
    if cnt["unlabeled"]:
        lines.append(f"  ! 정답 미지정 {cnt['unlabeled']}문항은 평가에서 제외됨: {', '.join(result['unlabeled_ids'])}")
    if len(result["by_category"]) > 1:
        lines.append("  범주별:")
        for cat, v in result["by_category"].items():
            lines.append(f"    {cat or '(범주 없음)':<14s} n={v['n']:<3d} Hit@{k} {v[f'hit_rate@{k}']:.2f}  Recall@{k} {v[f'recall@{k}']:.2f}  MRR {v['mrr']:.2f}")
    misses = [r for r in result["questions"] if r["first_hit_rank"] is None or r["first_hit_rank"] > k]
    if misses:
        lines.append(f"  Top-{k}에서 놓친 질문 {len(misses)}개" + (f" (앞 {FAILURE_LIMIT}개)" if len(misses) > FAILURE_LIMIT else "") + ":")
        for r in misses[:FAILURE_LIMIT]:
            got = ", ".join(r["retrieved"][:k]) or "(없음)"
            rank = f"{r['first_hit_rank']}위" if r["first_hit_rank"] else "상위 결과에 없음"
            lines.append(f"    [{r['id']}] {r['question'][:40]}  정답 {r['relevant']} → {rank}; 검색 {got}")
    return "\n".join(lines)


def comparison_table(results: Sequence[dict]) -> str:
    """기획서 9.4에 옮겨 적기 좋은 마크다운 표."""
    k = results[0]["config"]["k"]
    rows = ["| 실험 | Hit Rate@%d | Recall@%d | MRR | 근거 없음 빈 결과율 |" % (k, k), "|---|---|---|---|---|"]
    for r in results:
        m = r["metrics"]
        na = f"{r['no_evidence_rate']:.2f}" if r["no_evidence_rate"] is not None else "-"
        rows.append(f"| {r['experiment']} | {m[f'hit_rate@{k}']:.3f} | {m[f'recall@{k}']:.3f} | {m['mrr']:.3f} | {na} |")
    return "\n".join(rows)


def save_result(result: dict, out_dir: Path | str | None = None) -> Path:
    directory = Path(out_dir or RESULTS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"{result['experiment']}_k{result['config']['k']}_{stamp}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="검색 평가 (Hit Rate / Recall / MRR)")
    ap.add_argument("--experiment", default="hybrid_filter", choices=(*EXPERIMENTS, "all"))
    ap.add_argument("--k", type=int, default=config.DEFAULT_TOP_K, help="강조할 K (3·5·10은 항상 계산)")
    ap.add_argument("--questions", default=str(QUESTIONS_PATH), help="질문 세트(jsonl)")
    ap.add_argument("--out-dir", default=str(RESULTS_DIR), help="결과 JSON 저장 폴더")
    ap.add_argument("--today", help="신청기한 기준일(ISO). 정하면 재현 가능한 평가가 된다. 기본은 오늘")
    ap.add_argument("--no-save", action="store_true", help="결과 파일을 저장하지 않는다")
    args = ap.parse_args(argv)

    questions = load_questions(args.questions)
    if not questions:
        raise SystemExit(f"질문이 없습니다: {args.questions}")

    if args.experiment != "baseline":
        from src.index import vector_store

        if vector_store.count() == 0:
            raise SystemExit("색인이 비어 있습니다. 먼저: python -m src.index.vector_store --sample  (또는 A의 파이프라인 결과 색인)")

    names = list(EXPERIMENTS) if args.experiment == "all" else [args.experiment]
    results = []
    for name in names:
        try:
            result = run_experiment(name, questions, make_search_fn(name, args.today), args.k, args.today)
        except NotImplementedError:
            if args.experiment == "all":
                print(f"[건너뜀] {name}: 아직 구현되지 않았습니다 (src/retrieve/baseline.py)\n")
                continue
            raise SystemExit(f"{name}은 아직 구현되지 않았습니다 (src/retrieve/baseline.py)")
        except ValueError as e:
            raise SystemExit(str(e))
        print(format_report(result) + "\n")
        if not args.no_save:
            print(f"  저장: {save_result(result, args.out_dir)}\n")
        results.append(result)

    if len(results) > 1:
        print("기획서 9.4에 옮겨 적을 비교표\n\n" + comparison_table(results))


if __name__ == "__main__":
    main(sys.argv[1:])
