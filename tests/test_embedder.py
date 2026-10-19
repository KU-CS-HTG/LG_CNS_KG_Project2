"""B: index/embedder.py. 실제 모델(약 2.2GB) 없이도 돌도록 가짜 모델로 동작을 검증한다.

실제 모델 검증(의미가 맞는지)은 RUN_REAL_EMBEDDING=1일 때만 실행한다: RUN_REAL_EMBEDDING=1 python -m pytest tests/test_embedder.py
"""
import hashlib
import math
import os

import numpy as np
import pytest

from src.common import config
from src.index import embedder

DIM = 8


class FakeModel:
    """텍스트 해시로 결정적인 벡터를 만든다. 호출 인자를 기록해 접두·배치·정규화를 확인한다."""

    def __init__(self):
        self.calls = []

    def encode(self, texts, batch_size, normalize_embeddings, convert_to_numpy, show_progress_bar):
        self.calls.append({"texts": list(texts), "batch_size": batch_size, "normalize": normalize_embeddings})
        out = []
        for t in texts:
            seed = int(hashlib.md5(t.encode("utf-8")).hexdigest()[:8], 16)
            v = np.random.default_rng(seed).normal(size=DIM)
            out.append(v / np.linalg.norm(v) if normalize_embeddings else v)
        return np.array(out)

    def get_sentence_embedding_dimension(self):
        return DIM


@pytest.fixture
def fake(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(embedder, "_get_model", lambda: model)
    return model


def test_empty_input(fake):
    assert embedder.embed_texts([]) == []
    assert fake.calls == []                                   # 빈 입력이면 모델을 부르지 않는다


def test_shape_and_order(fake):
    texts = ["청년월세 지원", "구직촉진수당", "청년월세 지원"]
    vecs = embedder.embed_texts(texts)
    assert len(vecs) == 3 and all(len(v) == DIM for v in vecs)
    assert isinstance(vecs, list) and isinstance(vecs[0], list) and isinstance(vecs[0][0], float)
    assert vecs[0] == vecs[2] and vecs[0] != vecs[1]          # 순서가 보존되고 같은 텍스트는 같은 벡터


def test_normalized_and_batch_size(fake):
    vecs = embedder.embed_texts(["a", "b"])
    assert fake.calls[0]["normalize"] is True
    assert fake.calls[0]["batch_size"] == config.EMBEDDING_BATCH_SIZE
    assert all(math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-6) for v in vecs)


def test_no_prefix_for_bge(fake, monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "BAAI/bge-m3")
    embedder.embed_texts(["문서"], kind="passage")
    embedder.embed_query("질문")
    assert fake.calls[0]["texts"] == ["문서"] and fake.calls[1]["texts"] == ["질문"]


def test_e5_prefixes(fake, monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "intfloat/multilingual-e5-large")
    embedder.embed_texts(["문서"])                              # 기본은 passage
    embedder.embed_query("질문")
    assert fake.calls[0]["texts"] == ["passage: 문서"]
    assert fake.calls[1]["texts"] == ["query: 질문"]


def test_embed_query_returns_flat_vector(fake):
    v = embedder.embed_query("월세가 부담돼요")
    assert len(v) == DIM and isinstance(v[0], float)


def test_invalid_kind(fake):
    with pytest.raises(ValueError):
        embedder.embed_texts(["x"], kind="document")


def test_embedding_dim(fake):
    assert embedder.embedding_dim() == DIM


@pytest.mark.skipif(not os.environ.get("RUN_REAL_EMBEDDING"), reason="실제 모델 테스트는 RUN_REAL_EMBEDDING=1일 때만")
def test_real_model_semantics(sample_chunks):
    """실제 모델로 '월세가 부담돼요'가 월세 지원 조각에 더 가깝고 기초연금보다 먼저인지 확인."""
    by_name = {c.service_name: c for c in sample_chunks if c.section == "지원대상"}
    docs = [by_name["청년월세 지원(서울)"].text, by_name["기초연금"].text]
    d_vecs, q = embedder.embed_texts(docs), embedder.embed_query("월세가 부담돼요")
    sim = [sum(a * b for a, b in zip(q, d)) for d in d_vecs]
    assert sim[0] > sim[1]
    assert len(q) == embedder.embedding_dim()
