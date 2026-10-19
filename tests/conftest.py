"""구현 전(NotImplementedError)인 테스트는 실패가 아니라 skip으로 표시한다.
구현하면 같은 테스트가 자동으로 실제 검증을 시작한다."""
import hashlib

import numpy as np
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


# --- B: 벡터 저장소·검색 테스트용 ---------------------------------------------
FAKE_DIM = 256


def _fake_vector(text: str) -> list[float]:
    """글자 2-gram을 해시해 만든 가짜 임베딩. 같은 글자가 겹칠수록 가까워져서(결정적) 실제 모델 없이도 검색 테스트가 된다."""
    import re

    v = np.zeros(FAKE_DIM)
    for word in re.findall(r"[0-9A-Za-z가-힣]+", text.lower()):
        grams = [word] if len(word) == 1 else [word[i : i + 2] for i in range(len(word) - 1)]
        for g in grams:
            v[int(hashlib.md5(g.encode("utf-8")).hexdigest()[:8], 16) % FAKE_DIM] += 1.0
    v[0] += 1e-3                                              # 영벡터 방지
    return (v / np.linalg.norm(v)).tolist()


@pytest.fixture
def fake_embedder(monkeypatch):
    """실제 모델(약 2.2GB) 없이 vector_store를 시험한다. embedder 모듈 함수를 가짜로 바꾼다."""
    from src.index import embedder

    monkeypatch.setattr(embedder, "embed_texts", lambda texts, kind="passage": [_fake_vector(t) for t in texts])
    monkeypatch.setattr(embedder, "embed_query", lambda text: _fake_vector(text))
    monkeypatch.setattr(embedder, "embedding_dim", lambda: FAKE_DIM)


@pytest.fixture
def tmp_vectorstore(tmp_path, monkeypatch):
    """테스트마다 임시 폴더에 벡터 저장소를 만든다 (data/vectorstore를 건드리지 않음)."""
    pytest.importorskip("chromadb", reason="pip install -r requirements.txt 필요")
    pytest.importorskip("rank_bm25", reason="pip install -r requirements.txt 필요")
    from src.common import config
    from src.index import vector_store

    monkeypatch.setattr(config, "VECTORSTORE_DIR", tmp_path / "vectorstore")
    vector_store.reset_cache()
    yield tmp_path / "vectorstore"
    vector_store.reset_cache()


@pytest.fixture
def indexed_sample(fake_embedder, tmp_vectorstore, sample_chunks):
    """샘플 조각 48개를 임시 저장소에 색인한 상태. 색인된 Chunk 목록을 돌려준다."""
    from src.index import vector_store

    vector_store.build_index(sample_chunks)
    return sample_chunks
