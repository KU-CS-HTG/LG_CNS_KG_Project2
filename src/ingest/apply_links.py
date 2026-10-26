"""W3: serviceDetail 원본의 '온라인신청사이트URL'·'접수기관명'을 서비스ID별 대응표로 만든다 (docs/WORK_ITEMS.md W3).

A의 정규화(normalize.py)는 이 두 필드를 저장하지 않아 DB·색인에 없다(2026-10-26 확인: 원본 10,964건 중 2,004건에
URL이 있음). 수집 경로·DB·색인을 바꾸지 않으려고 이미 받아 둔 원본(data/raw/<실행ID>/serviceDetail-*.json)에서
따로 뽑아 data/raw/apply_links.json(원본과 같이 커밋하지 않음)에 둔다. 원본이 없는 PC에서는 대응표가 없고,
src/generate/apply_link.py가 신청방법 원문의 URL만으로 찾는다.

    python -m src.ingest.apply_links            # 가장 최근 수집 실행의 원본으로 대응표를 만든다
    python -m src.ingest.apply_links --run <실행ID>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.common import config

LINKS_PATH = config.RAW_DIR / "apply_links.json"
URL_FIELD = "온라인신청사이트URL"
ORG_FIELD = "접수기관명"
ID_FIELD = "서비스ID"

_cache: dict[str, dict] | None = None


def latest_run(raw_dir: Path = config.RAW_DIR) -> Path | None:
    """serviceDetail 원본이 있는 가장 최근 수집 실행 폴더 (폴더 이름이 UTC 시각으로 시작해 이름순이 시간순)."""
    runs = sorted(p for p in raw_dir.iterdir() if p.is_dir() and any(p.glob("serviceDetail-0*.json"))) \
        if raw_dir.exists() else []
    return runs[-1] if runs else None


def build(run_dir: Path) -> dict[str, dict]:
    """{서비스ID: {"url": 원문 URL, "org": 접수기관명}}. 둘 다 비어 있는 서비스는 넣지 않는다.
    값은 원문 그대로 둔다 (주소 형식 확인·보정은 generate.apply_link가 한다)."""
    links: dict[str, dict] = {}
    for path in sorted(run_dir.glob("serviceDetail-0*.json")):
        rows = json.loads(path.read_text(encoding="utf-8")).get("data") or []
        for row in rows:
            sid = str(row.get(ID_FIELD) or "").strip()
            url, org = (str(row.get(URL_FIELD) or "").strip(), str(row.get(ORG_FIELD) or "").strip())
            if sid and (url or org):
                links[sid] = {"url": url, "org": org}
    return links


def save(links: dict[str, dict], path: Path = LINKS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(links, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def load(path: Path = LINKS_PATH) -> dict[str, dict]:
    """대응표 (처음 한 번 읽어 둔다). 파일이 없거나 읽을 수 없으면 빈 dict."""
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache


def main() -> None:
    ap = argparse.ArgumentParser(description="serviceDetail 원본에서 온라인 신청 URL·접수기관 대응표를 만든다")
    ap.add_argument("--run", help="data/raw 아래 수집 실행 폴더 이름 (기본: 가장 최근)")
    args = ap.parse_args()
    run_dir = config.RAW_DIR / args.run if args.run else latest_run()
    if run_dir is None or not run_dir.exists():
        raise SystemExit("serviceDetail 원본이 있는 수집 실행 폴더를 찾지 못했습니다 (data/raw/<실행ID>/).")
    links = build(run_dir)
    save(links)
    with_url = sum(1 for v in links.values() if v["url"])
    print(f"{run_dir.name}: 서비스 {len(links)}개 (URL {with_url}개) -> {LINKS_PATH}")


if __name__ == "__main__":
    main()
