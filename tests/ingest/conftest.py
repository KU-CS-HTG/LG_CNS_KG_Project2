"""A 전용 격리 및 미구현 실패 처리. 공용 스켈레톤 skip 훅은 수정하지 않는다."""
import pytest

from src.common import config
from src.ingest import storage


@pytest.fixture(autouse=True)
def isolated_a(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "services.db")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "GOV24_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(storage, "validation_root", lambda: tmp_path / "validation")


@pytest.fixture
def a_storage(tmp_path):
    return storage.StoragePaths(tmp_path / "test-state" / "services.db", tmp_path / "test-state" / "raw", "test").validated()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if call.excinfo is not None and call.excinfo.errisinstance(NotImplementedError):
        report = outcome.get_result()
        report.outcome = "failed"
        report.longrepr = call.excinfo.getrepr()
