"""python -m src.review <명령>: 이전·점검·통계·시험지 내보내기·검수 대기열."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

from src.common import config
from src.review import intake, legacy
from src.review.export import export_eval
from src.review.queue import build_queue
from src.review.store import DEFAULT_ROOT, ReviewStore


def load_services(db_path: str | Path) -> dict[str, dict]:
    """A의 SQLite에서 활성 제도를 읽기 전용으로 가져온다 (service_id -> 정규화된 레코드)."""
    db = Path(db_path)
    if not db.is_file():
        raise SystemExit(f"DB가 없습니다: {db}")
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        rows = con.execute("SELECT service_id, payload FROM services WHERE status='active'").fetchall()
    return {sid: json.loads(payload) for sid, payload in rows}


def hybrid_search_fn(modes=("hybrid_filter", "vector", "bm25")):
    """여러 검색 방식의 상위 n개를 합쳐 후보로 쓴다 (한 방식이 놓친 제도를 덜 놓치도록). 필터는 hybrid_filter에만 적용된다."""
    from src.retrieve import hybrid

    def search(text: str, filters: dict, n: int) -> list[tuple[str, int, str]]:
        out = []
        for mode in modes:
            result = hybrid.search_with_info(text, dict(filters), n, mode)
            seen = []
            for chunk in result.chunks:
                if chunk.service_id not in seen:
                    seen.append(chunk.service_id)
            out += [(sid, rank, mode) for rank, sid in enumerate(seen, start=1)]
        return out

    return search


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.review", description="검수 저장소 도구 (docs/REVIEW_STORE.md)")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="검수 폴더 (기본 reviews/)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import-legacy", help="eval/questions.jsonl의 질문·정답을 이벤트로 옮기기 (다시 실행해도 중복 없음)")
    p.add_argument("--questions", default=str(config.EVAL_DIR / "questions.jsonl"))
    p.add_argument("--reviewer", default="legacy")
    p.add_argument("--db", default=str(config.DB_PATH))
    p = sub.add_parser("import-questions", help="새 질문 일괄 등록 (csv 또는 jsonl). 이미 있는 질문은 건너뜀")
    p.add_argument("file")
    p.add_argument("--reviewer", required=True, help="등록하는 사람 (reviews/events/<이름>.jsonl)")
    p.add_argument("--dry-run", action="store_true", help="쓰지 않고 결과만 미리 보기")
    sub.add_parser("validate", help="모든 이벤트 파일 검증 (오류가 있으면 종료 코드 1)")
    sub.add_parser("stats", help="질문·판정·사유 통계, 열린 메모, 활성 지침")
    p = sub.add_parser("agreement", help="두 검수자의 일치율과 kappa")
    p.add_argument("reviewer_a")
    p.add_argument("reviewer_b")
    p = sub.add_parser("export", help="판정 -> 평가 시험지(jsonl). 기한 지난 제도는 자동 제외, test는 한 번 만들면 덮어쓰지 않음")
    p.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    p.add_argument("--out", required=True)
    p.add_argument("--today", default=date.today().isoformat())
    p.add_argument("--db", default=str(config.DB_PATH))
    p.add_argument("--overwrite", action="store_true", help="dev/all 시험지만 덮어쓰기")
    p = sub.add_parser("queue", help="검수 대기열: 판정 없는 새 후보와 제도 내용이 바뀐 후보 (색인 필요)")
    p.add_argument("--n", type=int, default=30, help="질문마다 검색 방식별 상위 몇 개를 볼지")
    p.add_argument("--split", choices=("dev", "test"))
    p.add_argument("--today", default=date.today().isoformat())
    p.add_argument("--db", default=str(config.DB_PATH))
    p.add_argument("--limit", type=int, default=40, help="화면에 보여 줄 대기 항목 수")
    args = ap.parse_args(argv)

    store = ReviewStore(args.root)
    if args.cmd == "import-legacy":
        report = legacy.import_legacy(store, args.questions, args.reviewer, load_services(args.db))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    state = store.state()
    if args.cmd == "import-questions":
        report = intake.import_questions(store, args.file, args.reviewer, state=state, dry_run=args.dry_run)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["invalid"] else 0
    if args.cmd == "validate":
        for err in state.errors:
            print(f"! {err['file']}:{err['line']}: {err['error']}")
        print("문제 없음" if not state.errors else f"오류 {len(state.errors)}건")
        return 1 if state.errors else 0
    if args.cmd == "stats":
        print(json.dumps(state.stats(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "agreement":
        print(json.dumps(state.agreement(args.reviewer_a, args.reviewer_b), ensure_ascii=False, indent=2))
        return 0
    services = load_services(args.db)
    if args.cmd == "export":
        try:
            report = export_eval(state, split=args.split, today=args.today, services=services, path=args.out, overwrite=args.overwrite)
        except FileExistsError as exc:
            print(f"! 이미 있는 시험지입니다 ({exc}): {args.out}", file=sys.stderr)
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    result = build_queue(state, hybrid_search_fn(), services, today=args.today, n=args.n, split=args.split)
    print(f"새 후보·변경 {len(result['items'])}개 / 보류 {len(result['unsure'])}개 / 충돌 {len(result['conflicts'])}개 / 제외 {result['skipped']}")
    for item in result["items"][: args.limit]:
        print(f"  [{item['kind']:7}] {item['id']} #{item['rank']:<2} ({item['source']}) {item['service_id']} {item['name']}")
    for row in result["conflicts"]:
        print(f"  [충돌] {row['id']} {row['service_id']} {row['name']} {row['labels']}")
    for row in result["unsure"]:
        print(f"  [보류] {row['id']} {row['service_id']} {row['name']} {row['reasons']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
