import argparse
import json
from pathlib import Path

import httpx

from benefit_rag.config import Settings
from benefit_rag.db import make_engine
from benefit_rag.gov24 import FixtureAdapter, Gov24Adapter, SourceError
from benefit_rag.ingestion import ReplayAdapter, ingest


def main() -> None:
    parser = argparse.ArgumentParser(description="보조금24 수집 기반 (fixture / live / replay)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("fixture", "live"):
        child = sub.add_parser(name)
        child.add_argument("--page-size", type=int, default=2)
        child.add_argument("--max-pages", type=int, default=2)
        if name == "fixture":
            child.add_argument("--directory", type=Path, default=Path("tests/fixtures/gov24"))
    sub.add_parser("replay").add_argument("--run-id", required=True)
    args = parser.parse_args()
    settings = Settings()
    Path("data").mkdir(exist_ok=True)
    engine = make_engine(settings.database_url.get_secret_value())
    try:
        with httpx.Client() as client:
            if args.command == "replay":
                adapter = ReplayAdapter(engine, args.run_id)
                report = ingest(
                    engine,
                    adapter,
                    dataset_kind=adapter.dataset_kind,
                    page_size=adapter.page_size,
                    max_pages=adapter.max_pages,
                    replay_of=args.run_id,
                )
            else:
                adapter = (
                    FixtureAdapter(args.directory)
                    if args.command == "fixture"
                    else Gov24Adapter(
                        client,
                        settings.gov24_service_key.get_secret_value()
                        if settings.gov24_service_key
                        else "",
                    )
                )
                report = ingest(
                    engine,
                    adapter,
                    dataset_kind=("fixture" if args.command == "fixture" else "live"),
                    page_size=args.page_size,
                    max_pages=args.max_pages,
                )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(0 if report["status"] == "completed" else 2)
    except SourceError as exc:
        parser.exit(2, f"수집 실행 불가: {exc}\n")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
