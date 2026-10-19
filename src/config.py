"""프로젝트 공통 설정: 경로, API 키, 모델, 검색 파라미터."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
DB_PATH = PROCESSED_DIR / "services.db"          # 정형 데이터 + 청크 원문 (SQLite)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GOV24_API_KEY = os.getenv("GOV24_API_KEY", "")

# 임베딩: "openai"(기본) 또는 "bge"(로컬 BAAI/bge-m3). .env에 EMBEDDING_PROVIDER=bge 로 전환
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "openai").lower()
OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
BGE_MODEL = "BAAI/bge-m3"
# 임베딩 모델마다 벡터 차원이 달라서 벡터DB 폴더를 분리
CHROMA_DIR = ROOT / f"chroma_db_{EMBEDDING_PROVIDER}"
COLLECTION_NAME = "gov_service_chunks"

CHAT_MODEL = "gpt-4o-mini"

# 검색
SEARCH_MODES = ("sql", "bm25", "vector", "hybrid")
DEFAULT_MODE = "hybrid"
DEFAULT_TOP_K = 5
CHUNK_SIZE = 800          # 긴 항목(선정기준 등)을 Recursive로 자를 기준 글자 수
CHUNK_OVERLAP = 100
RRF_K = 60                # Hybrid의 Reciprocal Rank Fusion 상수


def require(key_name: str, value: str) -> str:
    """키가 비어 있으면 바로 알 수 있도록 에러를 낸다."""
    if not value:
        raise RuntimeError(f"{key_name}가 비어 있습니다. .env 파일을 확인하세요.")
    return value
