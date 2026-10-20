"""Test-only subprocess bootstrap: real B/Chroma, existing fake embedding pattern."""
import json
import sys
from pathlib import Path


def main():
    mode, path = sys.argv[1:3]
    if mode == "lock":
        from src.ingest.locking import file_lock
        with file_lock(Path(path), "busy"):
            print("locked", flush=True)
            sys.stdin.readline()
        return
    from src.common import config
    from src.index import embedder
    from tests.conftest import FAKE_DIM, _fake_vector
    embedder.embedding_dim = lambda: FAKE_DIM
    embedder.embed_texts = lambda texts, kind="passage": [_fake_vector(t) for t in texts]
    embedder.embed_query = _fake_vector
    if mode == "worker":
        from src.ingest.handoff_worker import main as worker_main
        return worker_main()
    request = json.loads(path)
    config.VECTORSTORE_DIR = Path(request["index_dir"])
    config.EMBEDDING_MODEL = request["model"]
    config.EMBEDDING_MAX_SEQ_LENGTH = request["max_seq_length"]
    from src.ingest.handoff import Settings, apply_pending
    from src.ingest.handoff_index import BIndexAdapter

    class Adapter(BIndexAdapter):
        def apply(self, event, chunks):
            if mode == "pause":
                print("protected", flush=True)
                sys.stdin.readline()
            super().apply(event, chunks)
            if mode == "crash":
                # A real process termination after index application, before ack.
                import os
                os._exit(9)

    result = apply_pending(Settings(**request), Adapter())
    print(json.dumps(result), flush=True)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
