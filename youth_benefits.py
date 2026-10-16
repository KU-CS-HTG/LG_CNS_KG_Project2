"""행정안전부 대한민국 공공서비스(혜택) 정보 API로 청년층 혜택 조회.

사전 준비
    pip install requests
    export GOV24_API_KEY="공공데이터포털에서 발급받은 일반 인증키(Decoding)"

사용 예
    python youth_benefits.py                        # 청년 키워드 혜택 목록 (콘솔 출력 + CSV 저장)
    python youth_benefits.py --field 주거 --limit 20 # 서비스분야에 '주거'가 포함된 것만
    python youth_benefits.py --detail 도시청년         # 목록 후 서비스명에 포함된 문구로 상세 조회
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

import requests

BASE_URL = "https://api.odcloud.kr/api/gov24/v3"
YOUTH_KEYWORDS = ["청년", "대학생", "사회초년생", "취업준비", "구직", "신혼", "만 19세", "만19세", "만 34세", "만34세", "만 39세", "만39세"]


def get_api_key(cli_key: str | None = None) -> str:
    key = cli_key or os.environ.get("GOV24_API_KEY")
    if not key:
        sys.exit("인증키가 없습니다. --key 옵션 또는 환경변수 GOV24_API_KEY 로 전달하세요.")
    return key


def call_api(endpoint: str, key: str, **params) -> dict:
    # requests가 인코딩하므로 '일반 인증키(Decoding)'를 사용해야 함
    resp = requests.get(
        f"{BASE_URL}/{endpoint}",
        params={"serviceKey": key, **params},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_services(key: str, per_page: int = 500, max_pages: int = 20):
    """서비스 목록을 페이지 단위로 순회하며 모두 가져온다."""
    page = 1
    while page <= max_pages:
        data = call_api("serviceList", key, page=page, perPage=per_page, returnType="JSON")
        rows = data.get("data", [])
        if not rows:
            return
        yield from rows
        if page * per_page >= data.get("totalCount", 0):
            return
        page += 1


def is_youth(row: dict) -> bool:
    text = " ".join(str(row.get(k, "")) for k in ("서비스명", "서비스목적요약", "지원대상", "선정기준"))
    return any(k in text for k in YOUTH_KEYWORDS)


def search_youth_benefits(key: str, field: str | None = None, limit: int | None = None):
    results = []
    for row in fetch_services(key):
        if not is_youth(row):
            continue
        if field and field not in str(row.get("서비스분야", "")):
            continue
        results.append(row)
        if limit and len(results) >= limit:
            break
    return results


def print_rows(rows):
    for i, r in enumerate(rows, 1):
        print(f"[{i}] {r.get('서비스명')}  ({r.get('서비스분야')} / {r.get('소관기관명')})")
        print(f"    대상: {str(r.get('지원대상', ''))[:100]}")
        print(f"    내용: {str(r.get('지원내용', ''))[:100]}")
        print(f"    신청: {r.get('신청방법')} | 기한: {r.get('신청기한')}")
        print(f"    URL : {r.get('상세조회URL')}")


def save_csv(rows, path="youth_benefits.csv"):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\nCSV 저장: {path}")


def show_detail(key: str, rows, name_part: str):
    for r in rows:
        if name_part in str(r.get("서비스명", "")):
            data = call_api("serviceDetail", key, **{"cond[서비스ID::EQ]": r["서비스ID"]})
            for d in data.get("data", []):
                print("\n=== 상세 ===")
                for k, v in d.items():
                    print(f"{k}: {v}")
            return
    print(f"'{name_part}' 가 포함된 서비스명을 찾지 못했습니다.")


def main():
    ap = argparse.ArgumentParser(description="청년 공공서비스(혜택) 조회")
    ap.add_argument("--key", help="인증키(Decoding). 없으면 환경변수 GOV24_API_KEY 사용")
    ap.add_argument("--field", help="서비스분야 필터 (예: 주거, 일자리, 교육, 금융)")
    ap.add_argument("--limit", type=int, help="최대 결과 수")
    ap.add_argument("--detail", help="서비스명에 이 문구가 포함된 항목의 상세 조회")
    args = ap.parse_args()

    key = get_api_key(args.key)
    rows = search_youth_benefits(key, args.field, args.limit)
    print(f"청년 관련 혜택 {len(rows)}건\n")
    print_rows(rows)
    save_csv(rows)
    if args.detail:
        show_detail(key, rows, args.detail)


if __name__ == "__main__":
    main()
