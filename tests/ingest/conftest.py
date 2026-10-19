"""A 전용 격리 및 미구현 실패 처리. 공용 스켈레톤 skip 훅은 수정하지 않는다."""
import pytest

from src.common import config


@pytest.fixture(autouse=True)
def isolated_a(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "services.db")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "GOV24_API_KEY", "synthetic-test-key")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if call.excinfo is not None and call.excinfo.errisinstance(NotImplementedError):
        report = outcome.get_result()
        report.outcome = "failed"
        report.longrepr = call.excinfo.getrepr()
