"""Process-local B configuration. No caller config/cache changes or downloads."""
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys


def main():
    # stdin contains paths/settings only, never credentials or raw API records.
    request = json.load(sys.stdin)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from src.common import config
    from src.ingest.handoff import Settings, apply_pending

    settings = Settings(**request).checked()
    # All changes below are restricted to this child process.
    config.VECTORSTORE_DIR = Path(settings.index_dir)
    config.EMBEDDING_MODEL = settings.model
    config.EMBEDDING_MAX_SEQ_LENGTH = settings.max_seq_length
    config.EMBEDDING_DEVICE = settings.device
    # Some libraries emit diagnostics to stdout. Keep protocol output unambiguous.
    with redirect_stdout(sys.stderr):
        from src.ingest.handoff_index import BIndexAdapter
        report = apply_pending(settings, BIndexAdapter())
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
