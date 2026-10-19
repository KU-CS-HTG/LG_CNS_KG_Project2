"""전원 공용 설정. 경로·모델명·상수는 여기서만 정의한다."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "services.db"
VECTORSTORE_DIR = ROOT / "data" / "vectorstore"
EVAL_DIR = ROOT / "eval"

GOV24_API_BASE = "https://api.odcloud.kr/api/gov24/v3"
GOV24_API_KEY = os.environ.get("GOV24_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# 사전기획서 8.1 초기 설계값 (실험으로 바뀌면 8.3에 기록하고 여기를 고친다)
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")  # 자원 부족 시 multilingual-e5
GENERATION_MODEL = "gpt-4o-mini"
DEFAULT_TOP_K = 5                # 평가에서 3 / 5 / 10 비교
MAX_RECOMMENDATIONS = 5          # 답변에 담는 제도 수 상한 (3~5개)
MAX_CHUNK_CHARS = 1000           # 긴 항목 Recursive 분할 기준 (수집 후 길이 측정해 조정)
