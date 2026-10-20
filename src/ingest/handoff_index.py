"""A-owned adapter to B's existing Chroma/BM25 implementation (no new index).

Only the worker imports this adapter in production. B's private storage codec,
tokenizer and BM25 corpus are checked here to avoid top-K/ranking assumptions.
"""
from collections import Counter
from pathlib import Path

import numpy as np

from src.common import config
from src.index import embedder, vector_store as index


class BIndexAdapter:
    def prepare(self, settings, *, bound):
        if (Path(config.VECTORSTORE_DIR).resolve() != Path(settings.index_dir)
                or config.EMBEDDING_MODEL != settings.model
                or config.EMBEDDING_MAX_SEQ_LENGTH != settings.max_seq_length):
            raise ValueError("worker_configuration_mismatch")
        if (Path(settings.index_dir) / "chroma.sqlite3").exists():
            import chromadb
            from chromadb.config import Settings as ChromaSettings
            client = chromadb.PersistentClient(path=settings.index_dir,
                                              settings=ChromaSettings(anonymized_telemetry=False))
            names = {collection.name for collection in client.list_collections()}
            if names and index._collection_name() not in names:
                raise ValueError("existing_index_model_not_found")
        # Check model dimension BEFORE deleting any stale service chunks.
        dimension = embedder.embedding_dim()
        col = index._collection()
        if col.count() and not bound and not settings.adopt_existing:
            raise ValueError("existing_index_requires_explicit_adoption")
        if (col.configuration.get("hnsw") or {}).get("space") != "cosine":
            raise ValueError("incompatible_index_distance")
        if col.count():
            embeddings = col.get(limit=1, include=["embeddings"])["embeddings"]
            if len(embeddings[0]) != dimension:
                raise ValueError("incompatible_embedding_dimension")
        return dimension

    def _documents(self):
        data = index._collection().get(include=["documents", "metadatas"])
        return {cid: (text, metadata) for cid, text, metadata in
                zip(data["ids"], data["documents"], data["metadatas"])}

    def capture(self, event):
        # Also prewarm the actual cache: verification must see its invalidation.
        index._bm25_index()
        return self._documents()

    def apply(self, event, chunks):
        if event["action"] == "remove" or not chunks:
            index.remove_services([event["service_id"]])
        else:
            index.build_index(chunks)

    def verify(self, event, chunks, before):
        sid = event["service_id"]
        expected = {c.chunk_id: (c.text, index._to_store_metadata(c)) for c in chunks}
        after = self._documents()
        actual = {cid: row for cid, row in after.items() if row[1]["service_id"] == sid}
        if actual != expected:
            raise RuntimeError("persistent_snapshot_mismatch")
        unrelated_before = {cid: row for cid, row in before.items() if row[1]["service_id"] != sid}
        unrelated_after = {cid: row for cid, row in after.items() if row[1]["service_id"] != sid}
        if unrelated_before != unrelated_after:
            raise RuntimeError("unrelated_services_modified")

        # BM25 is a derived in-memory corpus, rebuilt by B's version marker.
        # Exact corpus membership AND token frequencies test visibility even when
        # Okapi assigns zero/negative scores in a tiny/common-term corpus.
        bm25, ids = index._bm25_index()
        if set(ids) != set(after) or len(ids) != len(after):
            raise RuntimeError("bm25_membership_mismatch")
        for i, cid in enumerate(ids):
            if bm25.doc_freqs[i] != dict(Counter(index._tokenize(after[cid][0]))):
                raise RuntimeError("bm25_content_mismatch")

        queries = [c.text for c in chunks] or [row[0] for row in before.values()
                                              if row[1]["service_id"] == sid] or [sid]
        where = {"service_id": sid}
        for query in queries:
            # Service-scoped, all chunks: unrelated ranking cannot hide a miss.
            vector = index.vector_search(query, max(1, len(expected)), where=where)
            if {c.chunk_id for c, _ in vector} != set(expected):
                raise RuntimeError("vector_visibility_mismatch")
            if any(not np.isfinite(score) for _, score in vector):
                raise RuntimeError("invalid_vector_score")
            keyword = index.bm25_search(query, max(1, len(after)), where=where)
            scores = bm25.get_scores(index._tokenize(query)) if bm25 is not None else []
            eligible = {cid for cid, score in zip(ids, scores) if cid in expected and score > 0}
            if {c.chunk_id for c, _ in keyword} != eligible:
                raise RuntimeError("bm25_retrieval_mismatch")
            # Verify decoded results use the current documents and B's codec.
            for chunk, _ in vector + keyword:
                if chunk.to_dict() != index._chunk_from_store(chunk.chunk_id, *expected[chunk.chunk_id]).to_dict():
                    raise RuntimeError("retrieved_snapshot_mismatch")
        # Persistent documents + BM25 membership also prove absence of a removed
        # service without inferring absence from a coincidental top-K miss.
