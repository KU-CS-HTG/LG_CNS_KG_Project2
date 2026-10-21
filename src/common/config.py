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
EMBEDDING_DEVICE = os.environ.get("EMBEDDING_DEVICE") or None   # None이면 자동(cuda > mps > cpu)
EMBEDDING_BATCH_SIZE = 32        # 메모리가 부족하면 줄인다
INDEX_BATCH_SIZE = 64            # 벡터 저장소에 한 번에 임베딩해 넣는 조각 수
EMBEDDING_MAX_SEQ_LENGTH = 512   # 조각은 MAX_CHUNK_CHARS(1000자) 이내라 512 토큰이면 대부분 충분 (bge-m3는 최대 8192)
GENERATION_MODEL = "gpt-4o-mini"
DEFAULT_TOP_K = 5                # 평가에서 3 / 5 / 10 비교
HYBRID_CANDIDATES = 50           # Vector·BM25 각각에서 가져올 후보 수 (서비스 중복 제거 후 Top-K를 채우기 위해 넉넉히)
RRF_K = 60                       # Reciprocal Rank Fusion 상수 (순위 합산 시 상위 순위의 영향 정도)
HYBRID_WEIGHTS = (1.0, 0.2)      # (Vector, BM25) 가중치. eval.sweep 결과 BM25를 줄일수록 좋아져 0.2로 정함 (기획서 8.3)
LOCAL_PENALTY = 1.0                # 질문에 지역이 없을 때, 지자체(지역) 제도의 RRF 점수에서 빼는 값 (0이면 끔). 지역을 모르면 전국 제도를 먼저 안내한다(B·C 합의). 1.0은 지역 제도를 뒤로 보내는 값으로, 제도를 빼지는 않는다. eval.sweep: 0→1.0에서 Hit@5 0.47→0.67
FIELD_BOOST = 0.002              # 관심 분야(Filters.field)가 제도의 분야와 맞으면 RRF 점수에 더하는 값. 필터가 아니라 가산점이다 (0이면 끔).
                                 # 1/(RRF_K+순위) 기준으로 한 검색에서 약 9순위 차이. 실험(eval)으로 조정
MAX_RECOMMENDATIONS = 5          # 답변에 담는 제도 수 상한 (3~5개)
MAX_CHUNK_CHARS = 1000           # 긴 항목 Recursive 분할 기준 (수집 후 길이 측정해 조정)

# 데이터 범위: 청년층 (README 1.1). 아래 값은 제안이며 팀 합의 후 확정한다.
YOUTH_AGE_RANGE = (19, 39)       # 청년기본법은 만 19~34세, 지자체 조례는 만 18~39세까지 다양 -> 수집은 넓게
YOUTH_KEYWORDS = ["청년", "대학생", "대학원생", "사회초년생", "취업준비", "미취업", "구직"]
