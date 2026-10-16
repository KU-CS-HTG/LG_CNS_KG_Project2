# 후속 RAG 평가 계획

현재 측정한 것은 수집 기반 테스트뿐이다. 검색 품질·생성 정확도·latency 개선 수치는 없다. 아래는 **제안 프로토콜**이며 달성 결과나 목표치가 아니다.

동일한 실제 corpus snapshot, 프로그램 current revision 집합, raw 근거, tokenizer/모델 revision을 고정한다. 한국어 자연어 질문과 관련 program IDs를 사람이 라벨링하고 지역·범주·연령·정보 완전성으로 층화한다. 개발/평가 질의를 나눠 동의어·설정 조정이 평가 라벨을 누출하지 않게 한다. 합성 정책은 품질 corpus와 분리한다.

동일 질문/후보 범위에 dense-only, lexical-only, dense+BM25 RRF를 비교한다. 프로그램 단위 Recall@5/10/20은 관련 혜택을 놓치는 정도, MRR은 첫 관련 결과까지의 순위를 확인하는 데 사용한다. 관련 결과가 없는 질문은 Recall/MRR 평균과 별도로 no-result 판단을 평가한다. 라벨이 불완전한 query는 따로 표시한다. chunk-level recall과 프로그램 중복률은 진단 지표다. 평균 외에 범주별 분포와 실패 사례를 기록한다.

필수 시나리오는 프로필 결측, 소득 정의/단위 충돌, AND/OR/예외 복합 조건, 애매한 정책 문장, 적용연도 불일치, 만 나이 경계, 종료된 접수 기간, 상시 표기, 시행일과 신청일 혼동, 근거 부족, 결과 없음, 문서 내부 prompt injection이다. “관련” 라벨과 “검증된 조건 충족” 라벨을 분리한다.

조건 단위 삼값 혼동행렬, false satisfied/false not_satisfied, 필요한 상황에서 needs_confirmation을 유지한 비율을 보고한다. 정책의 미표현 조건과 공식 심사 결과는 추정하지 않는다. missing을 false로 바꾸거나 OR의 실패 하나를 전체 탈락으로 만드는 오류는 별도 중대 오류로 기록한다.

생성 평가에는 claim→document/raw pointer 존재, 인용된 문장의 실제 뒷받침 여부, 허용 공식 URL 일치, 정책 금액/기간/절차 생성 여부, 데이터 갱신 시점 표시, 미확인 조건 누락 여부를 포함한다. 참조 ID가 존재해도 claim을 지원하지 않으면 citation correctness 실패다. 자동 검사와 사람의 근거 검토를 함께 사용한다.

동일 장비에서 cold/warm, corpus/index 크기, 질문 길이, batch/최대 토큰 수를 고정하고 수집·tokenization·embedding·검색·fusion·생성을 각각 측정한다. latency p50/p95, peak RAM/VRAM, API request/token 수, 캐시 적중 및 당시 과금 기준을 기록한다. 소량 샘플과 분포를 공개하고 유료/대형 모델 실험은 명시적으로 실행한다. reranker는 recall이 확보된 상태에서 ranking 오류가 남고 추가 latency/비용을 감수할 근거가 있을 때만 도입한다.
