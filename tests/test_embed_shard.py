"""B: embed_cache / embed_shard / embedder의 미리 계산한 벡터 사용. 가짜 모델과 임시 SQLite만 쓴다 (모델·네트워크 불필요)."""
import hashlib
import json

import numpy as np
import pytest

from src.common import config
from src.index import embed_cache, embed_shard, embedder
from src.ingest import storage, store

DIM = 8


class FakeModel:
    def __init__(self):
        self.texts = []

    def encode(self, texts, batch_size, normalize_embeddings, convert_to_numpy, show_progress_bar):
        self.texts.extend(texts)
        out = []
        for t in texts:
            seed = int(hashlib.md5(t.encode("utf-8")).hexdigest()[:8], 16)
            v = np.random.default_rng(seed).normal(size=DIM)
            out.append(v / np.linalg.norm(v))
        return np.array(out)

    def get_sentence_embedding_dimension(self):
        return DIM


@pytest.fixture
def fake(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(embedder, "_get_model", lambda: model)
    monkeypatch.setattr(config, "EMBEDDING_CACHE", None)
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "BAAI/bge-m3")
    embedder._cache = embedder._cache_source = None
    embedder.cache_stats.update(hits=0, misses=0)
    yield model
    embedder._cache = embedder._cache_source = None


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "services.db")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    paths = storage.StoragePaths(tmp_path / "state" / "services.db", tmp_path / "state" / "raw", "test").validated()
    services = [store.clean_service({"서비스ID": f"S{i:02d}", "서비스명": f"청년 지원 {i}", "지원대상": f"청년 대상 {i}",
                                    "지원내용": f"내용 {i}", "소관기관명": "기관", "상세조회URL": f"https://www.gov.kr/s/{i}"})
                for i in range(12)]
    store.upsert_services(services, storage=paths)
    return paths.db_path


def _vectors(texts):
    return embedder.embed_texts(texts, kind="passage")


# --- embed_cache -------------------------------------------------------------------------------
def test_cache_roundtrip_atomic_and_key_stability(tmp_path):
    c = embed_cache.VectorCache("m", 512, 3)
    k = embed_cache.text_key("m", "passage", "문장")
    c.add(k, [1, 0, 0])
    c.add(k, [1, 0, 0])                                                 # 같은 키·같은 벡터는 무시
    assert len(c) == 1 and k in c
    path = c.save(tmp_path / "a.npz")
    assert not (tmp_path / "a.npz.tmp").exists()
    loaded = embed_cache.VectorCache.load(path)
    assert loaded.meta == c.meta and np.allclose(loaded.get(k), [1, 0, 0])
    assert embed_cache.text_key("m", "passage", "문장") == k != embed_cache.text_key("m", "query", "문장")


def test_cache_rejects_conflicting_vectors_and_wrong_dim(tmp_path):
    c = embed_cache.VectorCache("m", 512, 3)
    c.add("k", [1, 0, 0])
    with pytest.raises(ValueError, match="서로 다릅니다"):
        c.add("k", [0, 1, 0])
    with pytest.raises(ValueError, match="차원"):
        c.add("x", [1, 0])
    c.add("k", [1, 0.0004, 0])                                           # GPU/CPU 수준의 작은 차이는 허용


def test_load_many_and_incompatible(tmp_path):
    a = embed_cache.VectorCache("m", 512, 2); a.add("k1", [1, 0]); a.save(tmp_path / "a.npz")
    b = embed_cache.VectorCache("m", 512, 2); b.add("k2", [0, 1]); b.save(tmp_path / "b.npz")
    both = embed_cache.VectorCache.load_many(tmp_path)
    assert len(both) == 2
    other = embed_cache.VectorCache("other-model", 512, 2); other.add("k3", [1, 0]); other.save(tmp_path / "c.npz")
    with pytest.raises(ValueError, match="model"):
        embed_cache.VectorCache.load_many(tmp_path)


# --- embedder가 캐시를 쓰는지 ----------------------------------------------------------------------
def test_embedder_uses_cache_and_skips_model(fake, tmp_path, monkeypatch):
    texts = ["가", "나", "다"]
    expected = _vectors(texts)
    c = embed_cache.VectorCache(config.EMBEDDING_MODEL, config.EMBEDDING_MAX_SEQ_LENGTH, DIM)
    for t, v in zip(texts, expected):
        c.add(embed_cache.text_key(config.EMBEDDING_MODEL, "passage", t), v)
    c.save(tmp_path / "all.npz")

    fake.texts.clear()
    monkeypatch.setattr(config, "EMBEDDING_CACHE", str(tmp_path / "all.npz"))
    got = _vectors(["나", "라", "가"])                                       # 캐시 2 + 새 텍스트 1, 순서 보존
    assert fake.texts == ["라"]                                          # 없는 것만 모델이 계산
    assert np.allclose(got[0], expected[1], atol=1e-6) and np.allclose(got[2], expected[0], atol=1e-6)
    assert embedder.cache_stats["hits"] == 2
    fake.texts.clear()
    _vectors(["가", "나"])
    assert fake.texts == []                                              # 전부 캐시면 모델을 부르지 않는다
    assert embedder.embed_texts(["질문"], kind="query") and fake.texts == ["질문"]   # 질문(query)은 캐시 대상이 아니다


def test_embedder_rejects_cache_for_other_model(fake, tmp_path, monkeypatch):
    c = embed_cache.VectorCache("other-model", config.EMBEDDING_MAX_SEQ_LENGTH, DIM)
    c.add("k", np.ones(DIM)); c.save(tmp_path / "x.npz")
    monkeypatch.setattr(config, "EMBEDDING_CACHE", str(tmp_path / "x.npz"))
    with pytest.raises(ValueError, match="embedding_cache_incompatible"):
        _vectors(["가"])


# --- embed_shard -----------------------------------------------------------------------------------
def test_shards_partition_pending(db):
    pending = embed_shard.load_pending(db)
    assert len(pending) == 12
    parts = [embed_shard.shard_texts(pending, i, 3) for i in range(3)]
    everything = embed_shard.shard_texts(pending, 0, 1)
    assert sorted(t for p in parts for t in p) == sorted(everything)    # 빠짐도 겹침도 없다
    assert all(parts)                                                   # 구간마다 일이 있다 (12개 서비스)
    assert embed_shard.parse_shard("1/3") == (1, 3)
    for bad in ("3/3", "a/b", "0/0", "-1/3"):
        with pytest.raises(SystemExit):
            embed_shard.parse_shard(bad)


def test_run_resume_merge_coverage_and_handoff_use(fake, db, tmp_path, monkeypatch):
    out = [tmp_path / f"part{i}.npz" for i in range(3)]
    for i in range(3):
        embed_shard.main(["run", "--shard", f"{i}/3", "--out", str(out[i]), "--db-path", str(db), "--batch", "4", "--save-every", "1"])
    computed = len(fake.texts)
    total = len(embed_shard.shard_texts(embed_shard.load_pending(db), 0, 1))
    assert computed == total                                            # 3대가 나눠 계산한 합 = 전체 (중복 계산 없음)

    fake.texts.clear()
    embed_shard.main(["run", "--shard", "0/3", "--out", str(out[0]), "--db-path", str(db)])
    assert fake.texts == []                                             # 이미 끝난 구간은 다시 계산하지 않는다

    all_file = tmp_path / "all.npz"
    embed_shard.main(["merge", *map(str, out), "--out", str(all_file)])
    report = embed_shard.coverage(db, all_file, 3)
    assert report["missing"] == 0 and report["covered"] == total
    assert [s["missing"] for s in report["shards"]] == [0, 0, 0]
    partial = embed_shard.coverage(db, out[1])
    assert 0 < partial["covered"] < total and partial["missing"] == total - partial["covered"]

    # 주 PC: 같은 텍스트를 다시 임베딩해도 모델을 부르지 않고, 벡터는 직접 계산한 것과 같다
    texts = embed_shard.shard_texts(embed_shard.load_pending(db), 0, 1)
    direct = _vectors(texts)
    fake.texts.clear()
    monkeypatch.setattr(config, "EMBEDDING_CACHE", str(all_file))
    cached = _vectors(texts)
    assert fake.texts == [] and np.allclose(direct, cached, atol=1e-6)


def test_run_resumes_after_partial_file(fake, db, tmp_path):
    out = tmp_path / "part.npz"
    texts = embed_shard.shard_texts(embed_shard.load_pending(db), 0, 1)
    calls = {"n": 0}

    def flaky(batch, kind):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt                                      # 중간에 끊김 (Ctrl+C)
        return embedder.embed_texts(batch, kind=kind)

    with pytest.raises(KeyboardInterrupt):
        embed_shard.compute(texts, out, batch=2, save_every=1, embed_fn=flaky, log=lambda *_: None)
    saved = len(embed_cache.VectorCache.load(out))
    assert saved == 4                                                    # 두 배치까지는 저장돼 있다
    fake.texts.clear()
    embed_shard.compute(texts, out, batch=2, save_every=1, log=lambda *_: None)
    assert len(fake.texts) == len(texts) - saved                         # 이어서 나머지만 계산
    assert len(embed_cache.VectorCache.load(out)) == len(texts)


def test_run_does_not_write_db(db):
    before = db.read_bytes()
    embed_shard.load_pending(db)
    assert db.read_bytes() == before


def test_build_index_with_cached_vectors_needs_no_model(fake, tmp_vectorstore, tmp_path, monkeypatch):
    """계산된 벡터 파일이 있으면 색인(build_index)이 모델 없이 끝나고, 검색 결과도 직접 계산한 색인과 같다."""
    from src.common.schema import Chunk
    from src.index import vector_store as vs

    chunks = [Chunk(f"S{i}#지원대상", f"S{i}", f"S{i} 월세 지원", "지원대상", f"S{i} 월세 지원\n청년 월세 {i}", {"agency": "국토교통부"}, "") for i in range(5)]
    texts = [c.text for c in chunks]
    embed_shard.compute(texts, tmp_path / "all.npz", batch=2, save_every=1, log=lambda *_: None)
    vs.build_index(chunks)
    direct = [c.chunk_id for c, _ in vs.vector_search("월세", 5)]
    vs.clear_index()

    fake.texts.clear()
    monkeypatch.setattr(config, "EMBEDDING_CACHE", str(tmp_path / "all.npz"))
    vs.build_index(chunks)
    assert fake.texts == []                                             # 색인 중 모델을 부르지 않음
    fake.texts.clear()
    assert [c.chunk_id for c, _ in vs.vector_search("월세", 5)] == direct   # 질문은 모델로 계산 (색인 벡터는 캐시)
