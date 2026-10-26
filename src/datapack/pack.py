"""데이터 팩 만들기·설치·상태 확인.

팩 = zip 하나: manifest.json + services.db(+핸드오프 연결 파일) + vectorstore/. 큰 zip은 공유 드라이브로 나누고, 작은
`data_pack/LATEST.json`(어떤 팩이 최신인지, 해시)만 git으로 공유한다. 팀원은 git pull 뒤 `status`로 새 팩이 있는지 보고
`install`로 바꾼다. 설치는 해시를 모두 검증한 뒤 기존 데이터를 백업해 두고 바꾸며, 실패하면 되돌린다."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

DB_NAME = "services.db"
STORE_NAME = "vectorstore"
SIDECAR = DB_NAME + ".handoff.json"          # A의 핸드오프가 'DB <-> 색인 연결'을 적어 두는 파일
MANIFEST = "manifest.json"
SCHEMA = 1
SKIP_SUFFIXES = (".lock", ".tmp")


class PackError(RuntimeError):
    """사용자에게 그대로 보여 줄 오류 메시지."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _pack_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _chromadb_version() -> str:
    try:
        import chromadb
        return chromadb.__version__
    except Exception:       # noqa: BLE001 — 없어도 팩은 만들 수 있다
        return ""


def _services_counts(db: Path) -> dict:
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        total, active = con.execute("SELECT COUNT(*), SUM(status='active') FROM services").fetchone()
    finally:
        con.close()
    return {"services_total": total or 0, "services_active": int(active or 0)}


def _snapshot_db(src: Path, dst: Path) -> None:
    """실행 중이어도 일관된 복사본을 만든다 (파일을 그대로 복사하면 -wal에 있는 최근 변경이 빠진다)."""
    s = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def build_pack(db_path: Path, store_dir: Path, out_dir: Path, *, created_by: str, notes: str = "",
               git_commit: str = "", embedding_model: str = "", where_hint: str = "",
               latest_path: Path | None = None) -> dict:
    """팩 zip을 out_dir에 만들고 manifest(dict)를 돌려준다. latest_path가 있으면 LATEST.json도 쓴다 (git으로 공유)."""
    db_path, store_dir, out_dir = Path(db_path), Path(store_dir), Path(out_dir)
    if not db_path.is_file():
        raise PackError(f"DB가 없습니다: {db_path}")
    if not store_dir.is_dir() or not any(store_dir.iterdir()):
        raise PackError(f"검색 색인 폴더가 없거나 비어 있습니다: {store_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    pack_id = _pack_id()
    zip_path = out_dir / f"data_pack_{pack_id}.zip"
    work = out_dir / f".build_{pack_id}"
    work.mkdir()
    try:
        snapshot = work / DB_NAME
        _snapshot_db(db_path, snapshot)
        entries: list[tuple[str, Path]] = [(DB_NAME, snapshot)]
        sidecar = Path(str(db_path) + ".handoff.json")
        if sidecar.is_file():
            entries.append((SIDECAR, sidecar))
        for p in sorted(store_dir.rglob("*")):
            if p.is_file() and not p.name.endswith(SKIP_SUFFIXES):
                entries.append((f"{STORE_NAME}/{p.relative_to(store_dir).as_posix()}", p))
        files = [{"path": name, "size": p.stat().st_size, "sha256": sha256_file(p)} for name, p in entries]
        manifest = {"schema": SCHEMA, "pack_id": pack_id, "created_at": _now(), "created_by": created_by, "notes": notes,
                    "git_commit": git_commit, "embedding_model": embedding_model, "chromadb_version": _chromadb_version(),
                    "counts": _services_counts(snapshot), "files": files}
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            z.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
            for name, p in entries:
                z.write(p, name)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    manifest["zip"] = {"file": zip_path.name, "size": zip_path.stat().st_size, "sha256": sha256_file(zip_path)}
    if latest_path:
        latest = {"pack_id": pack_id, "file": zip_path.name, "size": manifest["zip"]["size"], "sha256": manifest["zip"]["sha256"],
                  "created_by": created_by, "created_at": manifest["created_at"], "notes": notes, "where": where_hint,
                  "embedding_model": embedding_model, "counts": manifest["counts"]}
        Path(latest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(latest_path).write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def status(state_path: Path, latest_path: Path) -> dict:
    """{'state': 'up_to_date'|'update'|'none'|'no_latest', 'local': 설치된 팩 id, 'latest': 최신 팩 정보}."""
    latest, local = read_json(latest_path), read_json(state_path)
    if not latest:
        return {"state": "no_latest", "local": (local or {}).get("pack_id"), "latest": None}
    if not local:
        return {"state": "none", "local": None, "latest": latest}
    return {"state": "up_to_date" if local.get("pack_id") == latest["pack_id"] else "update", "local": local.get("pack_id"), "latest": latest}


def _safe_members(z: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = []
    for info in z.infolist():
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in Path(name).parts or ":" in name.split("/")[0]:
            raise PackError(f"위험한 경로가 든 파일입니다 (설치하지 않음): {name}")
        if not info.is_dir():
            members.append(info)
    return members


def install_pack(zip_path: Path, db_path: Path, store_dir: Path, state_path: Path, *, latest_path: Path | None = None,
                 expected_model: str = "", force: bool = False, backup_root: Path | None = None) -> dict:
    """zip을 검증해 db_path·store_dir 자리에 설치한다. 기존 데이터는 backup_root(기본: DB 옆)의 _backup_<시각>/ 으로 옮겨 둔다."""
    zip_path, db_path, store_dir, state_path = Path(zip_path), Path(db_path), Path(store_dir), Path(state_path)
    if not zip_path.is_file():
        raise PackError(f"팩 파일이 없습니다: {zip_path}")
    latest = read_json(latest_path) if latest_path else None
    if latest and not force and zip_path.name == latest.get("file") and sha256_file(zip_path) != latest.get("sha256"):
        raise PackError("받은 파일이 올려 둔 팩과 다릅니다 (다운로드가 끊겼거나 다른 파일). 다시 받으세요")
    stage = db_path.parent / f".datapack_stage_{os.getpid()}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            try:
                manifest = json.loads(z.read(MANIFEST))
            except (KeyError, json.JSONDecodeError) as exc:
                raise PackError("데이터 팩이 아닙니다 (manifest.json이 없거나 깨짐)") from exc
            members = {i.filename: i for i in _safe_members(z)}
            expected = {f["path"]: f for f in manifest.get("files", [])}
            if set(expected) - set(members):
                raise PackError(f"팩에 빠진 파일이 있습니다: {sorted(set(expected) - set(members))[:3]}")
            for name, meta in expected.items():
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(members[name]) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if target.stat().st_size != meta["size"] or sha256_file(target) != meta["sha256"]:
                    raise PackError(f"파일이 손상됐습니다: {name}")
        model = manifest.get("embedding_model") or ""
        if expected_model and model and model != expected_model and not force:
            raise PackError(f"임베딩 모델이 다릅니다 (팩 {model} / 이 컴퓨터 설정 {expected_model}). 질문 검색이 맞지 않게 됩니다. "
                            "설정을 맞추거나 --force")
        backup = Path(backup_root or db_path.parent) / f"_backup_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        moved: list[tuple[Path, Path]] = []
        try:
            for live in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm"), Path(str(db_path) + ".handoff.json"), store_dir):
                if live.exists():
                    backup.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(live), str(backup / live.name))
                    moved.append((live, backup / live.name))
            shutil.move(str(stage / DB_NAME), str(db_path))
            if (stage / SIDECAR).exists():
                shutil.move(str(stage / SIDECAR), str(Path(str(db_path) + ".handoff.json")))
            shutil.move(str(stage / STORE_NAME), str(store_dir)) if (stage / STORE_NAME).exists() else store_dir.mkdir(parents=True)
        except Exception as exc:
            for live, saved in moved:           # 설치가 중간에 실패하면 기존 데이터를 되돌린다
                if live.exists():
                    shutil.rmtree(live) if live.is_dir() else live.unlink()
                shutil.move(str(saved), str(live))
            if isinstance(exc, PermissionError):    # Windows: 앱·handoff가 색인 파일을 열고 있을 때
                raise PackError("파일이 사용 중이라 바꾸지 못했어요 (기존 데이터는 그대로예요). 앱·handoff·explain을 모두 끄고 다시 실행하세요") from exc
            raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    state = {"pack_id": manifest["pack_id"], "installed_at": _now(), "created_by": manifest.get("created_by", ""),
             "counts": manifest.get("counts", {}), "backup": str(backup) if moved else ""}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**state, "model": model, "chromadb_version": manifest.get("chromadb_version", "")}
