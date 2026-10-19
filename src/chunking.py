"""[A/B] 구조 기반 분할: 서비스 1건 → 항목별 청크 (긴 항목은 Recursive로 추가 분할)."""
from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.config import CHUNK_OVERLAP, CHUNK_SIZE
from src.schema import CHUNK_FIELDS, Service

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    # 행정 문서의 목록 기호(○, -, ㆍ) 앞에서 먼저 끊어 조건 하나가 반으로 잘리는 일을 줄인다
    separators=["\n\n", "\n○", "\n-", "\nㆍ", "\n", ". ", " ", ""],
)


def chunk_service(s: Service) -> list[dict]:
    """반환: [{chunk_id, service_id, field, text}, ...]

    text 앞에 서비스명과 항목명을 붙여, 청크만 봐도 어느 제도의 어떤 항목인지 알 수 있게 한다.
    """
    chunks = []
    for key, label in CHUNK_FIELDS.items():
        body = (s.get(key) or "").strip()
        if not body:
            continue
        for i, piece in enumerate(_splitter.split_text(body)):
            chunks.append({
                "chunk_id": f"{s['service_id']}-{key}-{i}",
                "service_id": s["service_id"],
                "field": label,
                "text": f"[{s['name']}] {label}\n{piece}",
            })
    return chunks
