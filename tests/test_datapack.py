"""데이터 팩: 만들기 -> 설치 -> 상태. 가짜 DB·색인 파일만 쓴다 (chromadb 불필요)."""
import json
import shutil
import sqlite3
import zipfile

import pytest

from src.datapack import pack


def make_data(root, n_services=3, marker="v1"):
    root.mkdir(parents=True, exist_ok=True)
    db = root / "services.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE services (service_id TEXT PRIMARY KEY, payload TEXT, status TEXT)")
    for i in range(n_services):
        con.execute("INSERT INTO services VALUES (?, ?, ?)", (f"S{i}", json.dumps({"marker": marker}), "active" if i else "closed"))
    con.commit()
    con.close()
    (root / "services.db.handoff.json").write_text(json.dumps({"index": "vectorstore", "marker": marker}), encoding="utf-8")
    store = root / "vectorstore"
    (store / "seg").mkdir(parents=True)
    (store / "chroma.sqlite3").write_bytes(marker.encode() * 100)
    (store / "seg" / "data.bin").write_bytes(bytes(range(256)) * 10)
    (store / "x.lock").write_text("lock")                                    # 잠금 파일은 팩에 넣지 않는다
    (store / "handoff_manifest.json").write_text(json.dumps({"marker": marker}), encoding="utf-8")
    return db, store


@pytest.fixture
def built(tmp_path):
    db, store = make_data(tmp_path / "boss")
    latest = tmp_path / "repo" / "data_pack" / "LATEST.json"
    m = pack.build_pack(db, store, tmp_path / "drive", created_by="taegyu", notes="첫 팩", embedding_model="BAAI/bge-m3",
                        where_hint="공유 드라이브/팩", latest_path=latest)
    return tmp_path, m, latest


def test_build_makes_zip_manifest_and_latest(built):
    tmp_path, m, latest = built
    zp = tmp_path / "drive" / m["zip"]["file"]
    assert zp.is_file() and m["counts"] == {"services_total": 3, "services_active": 2}
    names = set(zipfile.ZipFile(zp).namelist())
    assert {"manifest.json", "services.db", "services.db.handoff.json", "vectorstore/chroma.sqlite3",
            "vectorstore/seg/data.bin", "vectorstore/handoff_manifest.json"} <= names
    assert "vectorstore/x.lock" not in names                                   # 잠금 파일 제외
    info = json.loads(latest.read_text(encoding="utf-8"))
    assert info["pack_id"] == m["pack_id"] and info["sha256"] == m["zip"]["sha256"] and info["where"] == "공유 드라이브/팩"


def test_install_replaces_data_keeps_backup_and_writes_state(built):
    tmp_path, m, latest = built
    mate = tmp_path / "mate"
    old_db, old_store = make_data(mate, n_services=1, marker="old")
    state = mate / ".datapack.json"
    r = pack.install_pack(tmp_path / "drive" / m["zip"]["file"], old_db, old_store, state, latest_path=latest, expected_model="BAAI/bge-m3")
    assert pack._services_counts(old_db) == {"services_total": 3, "services_active": 2}
    assert (old_store / "handoff_manifest.json").read_text(encoding="utf-8") == json.dumps({"marker": "v1"})
    assert json.loads((mate / "services.db.handoff.json").read_text(encoding="utf-8"))["marker"] == "v1"
    backup = pack.Path(r["backup"])
    assert (backup / "vectorstore" / "chroma.sqlite3").read_bytes() == b"old" * 100 and (backup / "services.db").is_file()   # 이전 데이터 보관
    assert pack.status(state, latest)["state"] == "up_to_date" and not list(mate.glob(".datapack_stage_*"))


def test_install_on_a_machine_without_data(built):
    tmp_path, m, latest = built
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    r = pack.install_pack(tmp_path / "drive" / m["zip"]["file"], fresh / "services.db", fresh / "vectorstore", fresh / ".datapack.json", latest_path=latest)
    assert r["backup"] == "" and (fresh / "vectorstore" / "seg" / "data.bin").is_file()


def test_status_transitions(built, tmp_path):
    _, m, latest = built
    state = tmp_path / "state.json"
    assert pack.status(state, tmp_path / "none.json")["state"] == "no_latest"
    assert pack.status(state, latest)["state"] == "none"
    state.write_text(json.dumps({"pack_id": "old"}), encoding="utf-8")
    s = pack.status(state, latest)
    assert s["state"] == "update" and s["local"] == "old" and s["latest"]["pack_id"] == m["pack_id"]


def test_corrupted_download_is_rejected_before_touching_data(built):
    tmp_path, m, latest = built
    zp = tmp_path / "drive" / m["zip"]["file"]
    raw = bytearray(zp.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    zp.write_bytes(bytes(raw))
    mate = tmp_path / "mate"
    db, store = make_data(mate, marker="keep")
    with pytest.raises(pack.PackError, match="다릅니다"):
        pack.install_pack(zp, db, store, mate / "s.json", latest_path=latest)
    assert (store / "chroma.sqlite3").read_bytes() == b"keep" * 100            # 기존 데이터 그대로


def test_tampered_file_inside_zip_is_caught_by_per_file_hash(built, tmp_path):
    _, m, _ = built
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(tmp_path / "drive" / m["zip"]["file"]) as src, zipfile.ZipFile(bad, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            dst.writestr(info.filename, data + b"x" if info.filename == "services.db" else data)
    mate = tmp_path / "mate"
    db, store = make_data(mate, marker="keep")
    with pytest.raises(pack.PackError, match="손상"):
        pack.install_pack(bad, db, store, mate / "s.json")
    assert (store / "chroma.sqlite3").read_bytes() == b"keep" * 100


def test_zip_with_path_escape_is_refused(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("manifest.json", json.dumps({"pack_id": "x", "files": []}))
        z.writestr("../outside.txt", "x")
    with pytest.raises(pack.PackError, match="위험"):
        pack.install_pack(evil, tmp_path / "d" / "services.db", tmp_path / "d" / "vectorstore", tmp_path / "s.json")
    assert not (tmp_path / "outside.txt").exists()


def test_embedding_model_mismatch_needs_force(built):
    tmp_path, m, latest = built
    mate = tmp_path / "mate"
    db, store = make_data(mate, marker="keep")
    args = (tmp_path / "drive" / m["zip"]["file"], db, store, mate / "s.json")
    with pytest.raises(pack.PackError, match="임베딩 모델"):
        pack.install_pack(*args, latest_path=latest, expected_model="intfloat/multilingual-e5-large")
    assert pack.install_pack(*args, latest_path=latest, expected_model="intfloat/multilingual-e5-large", force=True)["pack_id"] == m["pack_id"]


def test_failed_swap_restores_the_previous_data(built, monkeypatch):
    tmp_path, m, latest = built
    mate = tmp_path / "mate"
    db, store = make_data(mate, marker="keep")
    real_move = shutil.move
    calls = {"n": 0}

    def flaky(src, dst, *a, **k):
        calls["n"] += 1
        if ".datapack_stage_" in str(src) and str(dst).endswith("vectorstore"):
            raise PermissionError("사용 중")                                    # 새 색인을 제자리에 넣을 때 실패
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(pack.shutil, "move", flaky)
    with pytest.raises(pack.PackError, match="사용 중"):
        pack.install_pack(tmp_path / "drive" / m["zip"]["file"], db, store, mate / "s.json", latest_path=latest)
    monkeypatch.setattr(pack.shutil, "move", real_move)
    assert (store / "chroma.sqlite3").read_bytes() == b"keep" * 100 and pack._services_counts(db)["services_total"] == 3
    assert json.loads((db.parent / "services.db.handoff.json").read_text(encoding="utf-8"))["marker"] == "keep"
    assert not (mate / "s.json").exists()                                       # 실패하면 설치 기록을 남기지 않는다
