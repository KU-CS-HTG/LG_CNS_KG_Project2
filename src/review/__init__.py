"""검수 저장소: 질문별 제도 판정·답변 메모·제도 주석·지침을 추가만 하는 이벤트로 쌓고, 시험지(eval)와 검수 대기열을 만든다.

설계와 필드 설명은 docs/REVIEW_STORE.md. 앱(검수 모드)은 ReviewStore의 add_* 메서드로 쓰고, 평가는 export 결과를 읽는다.
"""
from src.review.store import ReviewStore  # noqa: F401
