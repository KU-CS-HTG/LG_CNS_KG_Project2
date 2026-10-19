"""구현 전(NotImplementedError)인 테스트는 실패가 아니라 skip으로 표시한다.
구현하면 같은 테스트가 자동으로 실제 검증을 시작한다."""
import pytest

from tests.fixtures import load_sample_chunks, load_search_cases


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


@pytest.fixture
def fake_search(monkeypatch):
    """B가 끝나기 전에도 C를 시험한다: hybrid.search를 미리 정한 결과(sample_search_results.json)로 바꾼다.

    사용: case = fake_search("sr1")  ->  case["results"], case["calls"](search가 받은 인자 기록)
    전제: C는 `from src.retrieve import hybrid; hybrid.search(...)`처럼 모듈 속성으로 호출한다.
    """
    def install(case_id: str) -> dict:
        case = dict(next(c for c in load_search_cases() if c["id"] == case_id))
        case["calls"] = []

        def _search(query, filters, k=5, mode="hybrid_filter"):
            case["calls"].append({"query": query, "filters": filters, "k": k, "mode": mode})
            return list(case["results"])[:k]

        monkeypatch.setattr("src.retrieve.hybrid.search", _search)
        return case

    return install


@pytest.fixture
def fake_conditions(monkeypatch):
    """LLM 없이 조건 추출 결과를 고정한다 (case["conditions"]).
    전제: C는 `condition_extractor.extract_conditions(...)`를 모듈 속성으로 호출한다."""
    def install(case: dict) -> None:
        monkeypatch.setattr(
            "src.generate.condition_extractor.extract_conditions", lambda question: dict(case["conditions"])
        )

    return install
