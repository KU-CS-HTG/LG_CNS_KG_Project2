"""전원 공용 설정. 경로·모델명·상수는 여기서만 정의한다."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "services.db"
VECTORSTORE_DIR = ROOT / "data" / "vectorstore"

GOV24_API_BASE = "https://api.odcloud.kr/api/gov24/v3"
GOV24_API_KEY = os.environ.get("GOV24_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")
GENERATION_MODEL = "gpt-4o-mini"
DEFAULT_TOP_K = 5
