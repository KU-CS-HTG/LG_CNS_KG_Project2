"""[C. 생성] 검색된 제도 문서를 근거로 답변 생성"""
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from src.config import CHAT_MODEL, OPENAI_API_KEY, require
from src.schema import UserProfile

PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "너는 정부 지원제도 안내 도우미다. 반드시 [제도 목록]에 있는 내용만 근거로 답한다.\n"
     "- 각 제도마다 서비스명, 지원내용 요약, 신청방법, 상세 URL을 알려준다.\n"
     "- 목록에 없는 제도는 지어내지 않는다. 맞는 제도가 없으면 없다고 말한다.\n"
     "- 소득·가구 조건처럼 사용자 정보로 확인할 수 없는 조건은 '확인 필요'로 표시한다."),
    ("human",
     "[사용자] 나이 {age}세, 거주지 {region}\n"
     "[질문] {question}\n\n"
     "[제도 목록]\n{context}"),
])


def format_context(docs: list[Document]) -> str:
    return "\n\n---\n\n".join(
        f"{d.page_content}\n[URL] {d.metadata.get('url', '')}" for d in docs
    )


def generate(question: str, user: UserProfile, docs: list[Document]) -> str:
    if not docs:
        return "입력하신 조건에 맞는 제도를 찾지 못했습니다. 나이나 거주지를 다시 확인해 주세요."
    llm = ChatOpenAI(model=CHAT_MODEL, temperature=0,
                     api_key=require("OPENAI_API_KEY", OPENAI_API_KEY))
    chain = PROMPT | llm
    result = chain.invoke({
        "age": user["age"], "region": user["region"],
        "question": question, "context": format_context(docs),
    })
    return result.content
