"""임베딩 모델 선택 — .env 의 EMBEDDING_PROVIDER 로 openai / bge 전환."""
from src.config import BGE_MODEL, EMBEDDING_PROVIDER, OPENAI_API_KEY, OPENAI_EMBEDDING_MODEL, require


def get_embeddings():
    if EMBEDDING_PROVIDER == "openai":
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(model=OPENAI_EMBEDDING_MODEL,
                                api_key=require("OPENAI_API_KEY", OPENAI_API_KEY))
    if EMBEDDING_PROVIDER == "bge":
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError as e:
            raise RuntimeError("bge를 쓰려면 먼저: pip install -r requirements-bge.txt") from e
        # 첫 실행 때 모델(약 2GB)을 내려받는다. 코사인 유사도를 위해 정규화.
        return HuggingFaceEmbeddings(model_name=BGE_MODEL,
                                     encode_kwargs={"normalize_embeddings": True})
    raise ValueError(f"알 수 없는 EMBEDDING_PROVIDER: {EMBEDDING_PROVIDER} (openai 또는 bge)")
