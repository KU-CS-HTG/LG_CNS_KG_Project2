"""Explicit per-execution storage. No process-global config mutation for fixtures."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.common import config


def validation_root() -> Path:
    return config.ROOT / "data" / "a_validation"


def _contains(directory: Path, path: Path) -> bool:
    return directory == path or directory in path.parents


def _same_file(left: Path, right: Path) -> bool:
    return left == right or (left.exists() and right.exists() and left.samefile(right))


def _db_files(path: Path) -> tuple[Path, ...]:
    return (path, *(Path(str(path) + suffix) for suffix in
                   ("-wal", "-shm", "-journal", ".ingest.lock")))


@dataclass(frozen=True)
class StoragePaths:
    db_path: Path
    raw_dir: Path
    mode: str = "live"

    def validated(self) -> StoragePaths:
        """Resolve aliases and reject protected destinations BEFORE any writes."""
        if self.mode not in ("live", "offline", "validation", "test"):
            raise ValueError("invalid_execution_mode")
        result = StoragePaths(Path(self.db_path).resolve(), Path(self.raw_dir).resolve(), self.mode)
        if _contains(result.raw_dir, result.db_path):
            raise ValueError("db_and_raw_destinations_overlap")
        if result.mode != "live":
            live_db, live_raw = Path(config.DB_PATH).resolve(), Path(config.RAW_DIR).resolve()
            if _contains(live_raw, result.raw_dir) or _contains(result.raw_dir, live_raw):
                raise ValueError("isolated_raw_overlaps_live_raw")
            if any(_contains(result.raw_dir, p) for p in _db_files(live_db)):
                raise ValueError("isolated_raw_overlaps_live_db")
            for path in _db_files(result.db_path):
                if _contains(live_raw, path) or any(_same_file(path, p) for p in _db_files(live_db)):
                    raise ValueError("isolated_db_overlaps_live_storage")
            if any(result.raw_dir == p for p in _db_files(live_db)):
                raise ValueError("isolated_raw_overlaps_live_db")
        return result

    def summary(self) -> dict:
        return {"mode": self.mode, "db_path": str(self.db_path), "raw_dir": str(self.raw_dir)}


def resolve_storage(mode: str = "live", *, db_path: Path | None = None,
                    raw_dir: Path | None = None) -> StoragePaths:
    if mode == "live":
        return StoragePaths(db_path if db_path is not None else config.DB_PATH,
                            raw_dir if raw_dir is not None else config.RAW_DIR, mode).validated()
    from src.ingest.client import new_run_id
    root = validation_root() / f"{mode}-{new_run_id()}"
    return StoragePaths(db_path if db_path is not None else root / "services.db",
                        raw_dir if raw_dir is not None else root / "raw", mode).validated()
