"""[A. 데이터] 보조금24 API 원본 수집 → data/raw/*.json

사용법:
    python scripts/collect_gov24.py --pages 3 --per-page 100

※ 엔드포인트·필드명은 공공데이터포털 Swagger 명세로 반드시 확인 후 수정하세요.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import requests

sys.path.append(str(Path(__file__).resolve().parent.parent))
from src.config import GOV24_API_KEY, RAW_DIR, require  # noqa: E402

BASE_URL = "https://api.odcloud.kr/api/gov24/v3"   # TODO: 명세로 확인
ENDPOINTS = ["serviceList", "supportConditions"]     # 목록 + 지원조건


def fetch_page(endpoint: str, page: int, per_page: int) -> dict:
    res = requests.get(
        f"{BASE_URL}/{endpoint}",
        params={"serviceKey": require("GOV24_API_KEY", GOV24_API_KEY),
                "page": page, "perPage": per_page},
        timeout=15,
    )
    res.raise_for_status()
    return res.json()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--per-page", type=int, default=100)
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for endpoint in ENDPOINTS:
        rows = []
        for page in range(1, args.pages + 1):
            try:
                data = fetch_page(endpoint, page, args.per_page)
            except requests.RequestException as e:
                print(f"[{endpoint}] page {page} 실패: {e}")
                break
            rows.extend(data.get("data", []))
            print(f"[{endpoint}] page {page}: 누적 {len(rows)}건 / 전체 {data.get('totalCount')}")
            if len(data.get("data", [])) < args.per_page:
                break
            time.sleep(0.3)  # 서버 부담 줄이기

        out = RAW_DIR / f"{endpoint}.json"
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"저장: {out}")


if __name__ == "__main__":
    main()
