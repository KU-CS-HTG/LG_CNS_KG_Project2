"""[B. 검색] services.jsonl → Chroma 벡터DB 생성

사용법:
    python scripts/build_index.py                       # 실제 데이터
    python scripts/build_index.py --input data/sample/services_sample.jsonl   # 샘플로 테스트
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from langchain_core.documents import Document  # noqa: E402

from src.config import CHROMA_DIR, PROCESSED_DIR  # noqa: E402
from src.retriever import get_vectorstore  # noqa: E402
from src.schema import to_document_text, to_metadata  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(PROCESSED_DIR / "services.jsonl"))
    args = parser.parse_args()

    path = Path(args.input)
    if not path.exists():
        sys.exit(f"입력 파일이 없습니다: {path}")

    services = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    docs = [Document(page_content=to_document_text(s), metadata=to_metadata(s)) for s in services]

    if CHROMA_DIR.exists():  # 다시 만들 때 중복 방지
        shutil.rmtree(CHROMA_DIR)
    vs = get_vectorstore()
    vs.add_documents(docs, ids=[d.metadata["service_id"] for d in docs])
    print(f"{len(docs)}건 인덱싱 완료 → {CHROMA_DIR}")


if __name__ == "__main__":
    main()
