"""python -m src.datapack <명령>: 데이터 팩 만들기(조장)·상태 확인·설치(팀원). 절차는 docs/DATA_PACK.md."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from src.common import config
from src.datapack import pack

LATEST = config.ROOT / "data_pack" / "LATEST.json"
# git이 이미 무시하는 폴더(data/a_validation/) 아래에 둔다: 설치 기록과 이전 데이터 백업(수백 MB)이 실수로 커밋되지 않게
WORK = config.ROOT / "data" / "a_validation" / "datapack"
STATE = WORK / "state.json"


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "log", "-1", "--format=%h"], capture_output=True, text=True, timeout=10, cwd=config.ROOT).stdout.strip()
    except Exception:       # noqa: BLE001
        return ""


def _fmt_counts(c: dict) -> str:
    return f"제도 {c.get('services_active', '?'):,}개(활성)" if isinstance(c.get("services_active"), int) else "-"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.datapack", description="데이터 팩 (docs/DATA_PACK.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build", help="[조장] 내 DB·색인을 팩 zip으로 만들고 LATEST.json을 갱신 (앱·handoff를 끄고 실행)")
    p.add_argument("--out", required=True, help="zip을 만들 폴더 (공유 드라이브 동기화 폴더 등)")
    p.add_argument("--by", required=True, help="만든 사람 이름")
    p.add_argument("--notes", default="", help="이번 팩에서 바뀐 점 (팀원에게 보인다)")
    p.add_argument("--where", default="", help="팀원이 zip을 받을 곳 안내 (예: 공유 드라이브/데이터팩)")
    p.add_argument("--no-latest", action="store_true", help="LATEST.json을 갱신하지 않는다 (시험용)")
    sub.add_parser("status", help="내 데이터가 최신 팩인지 확인")
    p = sub.add_parser("install", help="[팀원] 받은 팩 zip으로 내 DB·색인을 바꾼다 (기존 데이터는 data/a_validation/datapack/_backup_*에 보관)")
    p.add_argument("zip", nargs="?", help="팩 zip 경로 (생략하면 --from 폴더나 DATAPACK_DIR에서 최신 팩을 찾는다)")
    p.add_argument("--from", dest="src_dir", help="zip이 있는 폴더 (기본 환경변수 DATAPACK_DIR)")
    p.add_argument("--force", action="store_true", help="해시·모델 불일치 경고를 무시 (권장하지 않음)")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "build":
            m = pack.build_pack(config.DB_PATH, config.VECTORSTORE_DIR, Path(args.out), created_by=args.by, notes=args.notes,
                                git_commit=_git_commit(), embedding_model=config.EMBEDDING_MODEL, where_hint=args.where,
                                latest_path=None if args.no_latest else LATEST)
            print(f"팩을 만들었어요: {Path(args.out) / m['zip']['file']}  ({m['zip']['size'] / 1e6:,.0f} MB, {_fmt_counts(m['counts'])})")
            if not args.no_latest:
                print(f"다음 순서로 팀에 알리세요:\n  1) 위 zip을 공유 드라이브에 올린다\n  2) git add {LATEST.relative_to(config.ROOT)} && git commit && git push\n"
                      "  3) 팀원은 git pull -> python -m src.datapack status -> python -m src.datapack install")
            return 0
        if args.cmd == "status":
            s = pack.status(STATE, LATEST)
            if s["state"] == "no_latest":
                print("올라온 데이터 팩 정보(data_pack/LATEST.json)가 없어요. git pull 했는지 확인하세요.")
            elif s["state"] == "none":
                L = s["latest"]
                print(f"이 컴퓨터는 아직 데이터 팩을 설치하지 않았어요. 최신 팩: {L['pack_id']} ({L['created_by']}, {_fmt_counts(L['counts'])})\n"
                      f"  받을 곳: {L.get('where') or '(안내 없음)'} / 파일 {L['file']}\n  메모: {L.get('notes') or '-'}\n  설치: python -m src.datapack install")
            elif s["state"] == "update":
                L = s["latest"]
                print(f"새 데이터 팩이 있어요: {L['pack_id']} (내 것 {s['local']}). 만든 사람 {L['created_by']}, {_fmt_counts(L['counts'])}\n"
                      f"  바뀐 점: {L.get('notes') or '-'}\n  받을 곳: {L.get('where') or '(안내 없음)'} / 파일 {L['file']}\n  설치: python -m src.datapack install")
            else:
                print(f"최신 데이터 팩({s['local']})을 쓰고 있어요.")
            return 0
        zip_path = Path(args.zip) if args.zip else None
        if zip_path is None:
            folder = Path(args.src_dir or os.environ.get("DATAPACK_DIR", ""))
            latest = pack.read_json(LATEST)
            if not latest or not args.src_dir and not os.environ.get("DATAPACK_DIR"):
                raise pack.PackError("zip 경로를 적거나(--from 폴더, 환경변수 DATAPACK_DIR) git pull로 data_pack/LATEST.json을 받으세요")
            zip_path = folder / latest["file"]
        r = pack.install_pack(zip_path, config.DB_PATH, config.VECTORSTORE_DIR, STATE, latest_path=LATEST,
                              expected_model=config.EMBEDDING_MODEL, force=args.force, backup_root=WORK)
        print(f"설치했어요: 팩 {r['pack_id']} ({_fmt_counts(r['counts'])}). 이전 데이터는 {r['backup'] or '(없음)'} 에 보관했어요 (문제없으면 지워도 됩니다).")
        print("검증: python -m src.generate.explain --check")
        return 0
    except pack.PackError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
