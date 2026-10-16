import argparse
import json
from pathlib import Path

import httpx
from pydantic import ValidationError

from benefit_rag.config import Settings
from benefit_rag.db import make_engine
from benefit_rag.evaluation_contract import EvaluationDataset, check_synthetic_evidence
from benefit_rag.gov24 import FixtureAdapter, Gov24Adapter, SourceError
from benefit_rag.ingestion import ReplayAdapter, ingest
from benefit_rag.profiling import profile_run


def main() -> None:
    parser = argparse.ArgumentParser(description="보조금24 수집·오프라인 검증")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("fixture", "live"):
        child = sub.add_parser(name)
        child.add_argument("--page-size", type=int, default=2)
        child.add_argument("--max-pages", type=int, default=2)
        if name == "fixture":
            child.add_argument("--directory", type=Path, default=Path("tests/fixtures/gov24"))
    sub.add_parser("replay").add_argument("--run-id", required=True)
    profile = sub.add_parser("profile", help="선택 run의 저장 raw만 분석 (API 미호출)")
    profile.add_argument("--run-id", required=True)
    profile.add_argument("--source", choices=["gov24"], default="gov24")
    profile.add_argument("--dataset-kind", choices=["fixture", "live"], required=True)
    profile.add_argument("--output", type=Path)
    validation = sub.add_parser("validate-evaluation", help="평가 주석 구조 검증 (정답 인증 아님)")
    validation.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "validate-evaluation":
        try:
            dataset = EvaluationDataset.model_validate_json(args.file.read_bytes())
        except OSError:
            parser.exit(2, "평가 파일 읽기 실패\n")
        except ValidationError as exc:
            # 질문·프로필·원문을 오류 메시지에 출력하지 않는다.
            errors = [{"location": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
            parser.exit(2, json.dumps({"status": "invalid", "errors": errors}) + "\n")
        checked = dataset.data_kind == "synthetic_contract"
        if checked:
            try:
                check_synthetic_evidence(dataset, Path.cwd())
            except ValueError as exc:
                parser.exit(2, f"합성 근거 검증 실패: {exc}\n")
        print(
            json.dumps(
                {
                    "status": "structural_validation_passed",
                    "data_kind": dataset.data_kind,
                    "case_count": len(dataset.cases),
                    "policy_truth_verified": False,
                    "evidence_storage_checked": checked,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    settings = Settings()
    Path("data").mkdir(exist_ok=True)
    engine = make_engine(settings.database_url.get_secret_value())
    try:
        if args.command == "profile":
            report = profile_run(
                engine, args.run_id, dataset_kind=args.dataset_kind, source=args.source
            )
            output = json.dumps(report, ensure_ascii=False, indent=2)
            if args.output:
                args.output.write_text(output + "\n", encoding="utf-8")
            print(output)
            return
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
