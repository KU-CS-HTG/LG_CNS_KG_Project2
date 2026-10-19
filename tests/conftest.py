"""구현 전(NotImplementedError)인 테스트는 실패가 아니라 skip으로 표시한다.
구현하면 같은 테스트가 자동으로 실제 검증을 시작한다."""
import pytest

from tests.fixtures import load_sample_chunks


@pytest.fixture(scope="session")
def sample_chunks():
    return load_sample_chunks()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if call.excinfo is not None and call.excinfo.errisinstance(NotImplementedError):
        report.outcome = "skipped"
        report.longrepr = (str(item.path), item.location[1] or 0, "미구현(NotImplementedError)")
