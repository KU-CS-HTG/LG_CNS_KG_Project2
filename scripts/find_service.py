"""[평가 준비] DB에서 제도를 찾아 서비스ID 확인 → eval/questions.jsonl 정답 지정에 사용

사용법:
    python scripts/find_service.py 주거급여
    python scripts/find_service.py 월세 --region 서울 --age 27
    python scripts/find_service.py 장려금 --detail          # 지원대상·선정기준 앞부분까지
"""
import argparse
import sys
from contextlib import closing
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from src import db  # noqa: E402
from src.regions import normalize_region  # noqa: E402
from src.schema import Conditions, age_text  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("keyword", help="서비스명·요약에 포함된 단어")
    parser.add_argument("--region", help="거주지 (예: 서울) — 해당 지역+전국만")
    parser.add_argument("--age", type=int, help="나이 — 연령 조건에 맞는 것만")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--detail", action="store_true")
    args = parser.parse_args()

    cond = Conditions(age=args.age, region=normalize_region(args.region))
    where, params = db.condition_sql(cond)
    sql = (f"SELECT * FROM services WHERE (name LIKE ? OR summary LIKE ?) AND {where} "
           f"ORDER BY (name LIKE ?) DESC, name LIMIT ?")
    like = f"%{args.keyword}%"
    with closing(db.connect()) as conn:
        rows = conn.execute(sql, [like, like, *params, like, args.limit]).fetchall()

    if not rows:
        print("결과 없음 — 다른 단어로 찾아보세요.")
        return
    for r in rows:
        print(f"{r['service_id']}  {r['name']}  | {r['agency']} | {r['region']} | "
              f"{age_text(r['min_age'], r['max_age'])}")
        if args.detail:
            for label, col in (("대상", "target"), ("기준", "criteria")):
                one_line = " ".join((r[col] or "").split())   # 줄바꿈 정리해서 한 줄로
                print(f"    {label}: {one_line[:120]}")
    print(f"\n{len(rows)}건 (최대 {args.limit}건 표시)")


if __name__ == "__main__":
    main()
