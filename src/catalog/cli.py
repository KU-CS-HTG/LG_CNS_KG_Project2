"""python -m src.catalog <명령>: 목록·반영·통계. 웹 화면은 python -m app.catalog_mode."""
from __future__ import annotations

import argparse
import json
import sys

from src.catalog import apply as apply_mod
from src.catalog import effective
from src.catalog.store import ROOT, CatalogStore


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.catalog", description="제도 목록 관리 (docs/CATALOG.md)")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--db", default=None, help="A의 SQLite (기본 data/services.db, 읽기 전용)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats", help="추가·수정·숨김 현황")
    p = sub.add_parser("apply", help="변경을 검색 색인에 반영 (임베딩 모델 필요, --dry-run은 미리 보기)")
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    from src.common import config
    from src.review.cli import load_services

    store = CatalogStore(args.root)
    overlay = store.overlay()
    if args.cmd == "stats":
        print(json.dumps({"직접 추가": len(overlay.manual), "수정한 API 제도": len(overlay.edits), "숨김": len(overlay.hidden),
                          "삭제": len(overlay.removed), "이벤트": len(overlay.events)}, ensure_ascii=False, indent=2))
        return 0
    api = load_services(args.db or config.DB_PATH)
    report = apply_mod.apply(api, overlay, dry_run=args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
