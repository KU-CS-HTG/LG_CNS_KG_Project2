"""[B. 검색] SQLite의 청크 → Chroma 벡터DB (BM25 인덱스는 검색할 때 메모리에서 자동 생성)

사용법:
    python scripts/build_index.py

.env의 EMBEDDING_PROVIDER(openai/bge)에 따라 chroma_db_openai/ 또는 chroma_db_bge/ 에 저장.
"""
import shutil
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from langchain_core.documents import Document  # noqa: E402

from src import db  # noqa: E402
from src.config import CHROMA_DIR, EMBEDDING_PROVIDER  # noqa: E402
from src.retriever import get_vectorstore  # noqa: E402

BATCH = 500   # 한 번에 임베딩할 청크 수 (API 요청 크기 제한 대비)


def main():
    chunks = db.all_chunks()
    services = db.get_services(list({c["service_id"] for c in chunks}))

    docs, ids = [], []
    for c in chunks:
        s = services[c["service_id"]]
        docs.append(Document(page_content=c["text"], metadata={
            "chunk_id": c["chunk_id"], "service_id": c["service_id"], "field": c["field"],
            "name": s["name"], "region": s["region"], "sigungu": s["sigungu"] or "",
            "min_age": int(s["min_age"]), "max_age": int(s["max_age"]),
            "category": s["category"] or "", "support_type": s["support_type"] or "",
        }))
        ids.append(c["chunk_id"])

    if CHROMA_DIR.exists():  # 다시 만들 때 중복 방지
        try:
            shutil.rmtree(CHROMA_DIR)
        except PermissionError:
            # Windows는 다른 프로그램이 연 파일을 지울 수 없다 (주로 실행 중인 Streamlit)
            sys.exit(f"❌ {CHROMA_DIR.name} 폴더를 다른 프로그램이 사용 중입니다.\n"
                     "   streamlit을 실행 중인 터미널에서 Ctrl+C로 종료한 뒤 다시 실행하세요.")
    vs = get_vectorstore()
    for i in range(0, len(docs), BATCH):
        vs.add_documents(docs[i:i + BATCH], ids=ids[i:i + BATCH])
        print(f"  {min(i + BATCH, len(docs))}/{len(docs)}")
    print(f"[{EMBEDDING_PROVIDER}] 청크 {len(docs)}개 인덱싱 완료 → {CHROMA_DIR}")


if __name__ == "__main__":
    main()
