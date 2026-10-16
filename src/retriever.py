"""[B. 검색] 조건 필터(정형) + 벡터 검색(비정형)

핵심: 나이·지역은 임베딩에 맡기지 않고 메타데이터 필터로 '확정' 거른다.
"""
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings

from src.config import (CHROMA_DIR, COLLECTION_NAME, EMBEDDING_MODEL,
                        OPENAI_API_KEY, require)
from src.schema import UserProfile


def get_vectorstore() -> Chroma:
    embeddings = OpenAIEmbeddings(model=EMBEDDING_MODEL,
                                  api_key=require("OPENAI_API_KEY", OPENAI_API_KEY))
    return Chroma(collection_name=COLLECTION_NAME,
                  embedding_function=embeddings,
                  persist_directory=str(CHROMA_DIR))


def build_filter(user: UserProfile) -> dict:
    """min_age <= 나이 <= max_age  AND  지역 ∈ {사용자 지역, 전국}"""
    return {
        "$and": [
            {"min_age": {"$lte": user["age"]}},
            {"max_age": {"$gte": user["age"]}},
            {"region": {"$in": [user["region"], "전국"]}},
        ]
    }


def search(query: str, user: UserProfile, k: int = 5) -> list[Document]:
    vs = get_vectorstore()
    return vs.similarity_search(query, k=k, filter=build_filter(user))
