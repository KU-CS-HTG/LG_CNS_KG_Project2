"""프로젝트 공통 설정: 경로, API 키, 모델 이름."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
CHROMA_DIR = ROOT / "chroma_db"
COLLECTION_NAME = "gov_services"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GOV24_API_KEY = os.getenv("GOV24_API_KEY", "")

EMBEDDING_MODEL = "text-embedding-3-small"
CHAT_MODEL = "gpt-4o-mini"


def require(key_name: str, value: str) -> str:
    """키가 비어 있으면 바로 알 수 있도록 에러를 낸다."""
    if not value:
        raise RuntimeError(f"{key_name}가 비어 있습니다. .env 파일을 확인하세요.")
    return value
