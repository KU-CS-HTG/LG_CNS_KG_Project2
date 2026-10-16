# 검색·답변 평가 계약

상태: **실험 설계 제안 + 데이터 계약/합성 근거 검증 도구 구현**. 실제 정책 평가셋과 검색·생성 성능 측정은 없다. 수집 테스트 통과를 RAG 성능으로 해석하지 않는다. SQL/BM25가 일부 질의에 충분한 결과도 유효하다.

동일한 실제 corpus snapshot, 프로그램 current revision 집합, raw 근거, tokenizer/모델 revision을 고정한다. 한국어 자연어 질문과 관련 program IDs를 사람이 라벨링하고 지역·범주·연령·정보 완전성으로 층화한다. 개발/평가 질의를 나눠 동의어·설정 조정이 평가 라벨을 누출하지 않게 한다. 합성 정책은 품질 corpus와 분리한다.

동일 질문/후보 범위에서 SQL-only, BM25-only, vector-only, BM25+vector+RRF를 비교한다. 아래의 통제 비교와 filtering ablation을 분리한다. K=5/10/20은 실험 설정 제안이며 UI의 최대 5개 표시와 별개다. 품질·향상 수치는 아직 없다.

## Pilot 데이터 제안

실제 원문·버전별 근거 확보 후 한국어 질의 **60개**를 제안한다. development 24개, held_out 36개이며 달성 수량이 아니다. 취업·구직/주거·이사/결혼/출산·양육 각 12개와 no-result·insufficient-evidence 12개를 시작 구성으로 한다. 태그는 중첩 가능하며 exact_name, everyday_language, incomplete_profile, numerical_boundary, compound_condition, exception, closed_period, unknown_period를 반드시 포함한다. 작은 검증 cohort와 개인 전체 제품 범위를 구분한다. 기업 지원은 제외한다.

paraphrase group 전체를 같은 split에 둔다. 도구는 동일 group ID 또는 동일 질문의 cross-split 사용을 거부하지만 의미적 near-duplicate는 사람이 검토한다. development로 설정을 고르고 held_out을 동결한다. 실제 원문을 사람이 주석·독립 검토하고 LLM 답변만을 ground truth로 쓰지 않는다.

## 기계 판독 데이터 계약

`evaluation_contract.py`의 Pydantic 모델이 기준이다. 최상위는 schema_version=evaluation-v1, data_kind, notice_ko, corpus_snapshot, cases다. snapshot은 source, fixture/live namespace, program당 한 frozen program_version_id 목록을 가진다. 후속 실제 실험 manifest에는 filter/tokenizer/model/prompt revision도 동결한다. 프로파일 hash는 raw 관측 범위이며 평가 corpus snapshot과 동일하지 않다.

| case 필드 | 의미 |
|---|---|
| query_id / question_ko | 고유 ID / 한국어 원질문 |
| split / paraphrase_group_id | development 또는 held_out / 누출 방지 질의군 |
| confirmed_profile | 확인한 age, residence_region, employment_status, household_size/composition, income, interests만. 모델 추정·정밀주소·주민등록번호는 없음 |
| evaluation_date | 명시적 ISO 날짜, 적용 timezone Asia/Seoul |
| corpus_snapshot_id / scenario_tags | 동결 버전 집합 / 층화 태그 |
| relevant_program_ids | 관련성 라벨이며 자격 충족·지급 확정 라벨 아님 |
| supporting_evidence_references | evidence ID, program/version, raw ID/SHA256, nullable document ID, JSON pointer, 시작/끝 offset, 원문 발췌 |
| expected_condition_findings | condition ID/원문, program/version, 삼값, validation_status, evidence IDs. 미검증/모호/결측은 needs_confirmation |
| expected_application_availability | program/version, nullable application_round_id, open/upcoming/closed/ongoing/unknown, evidence IDs |
| expected_response_facts | fact ID/내용·program/version·근거 IDs |
| forbidden_or_unsupported_claims | 없는 금액/URL/기간, 확정 자격, 전국 완전성 등 금지/미지원 주장 |
| annotation_status | draft/reviewed. 구조 통과가 reviewed 승격은 아님 |
| label_completeness | complete/partial/unlabeled. unlabeled에는 정답 라벨을 넣지 않음 |
| review_notes | 주석 범위·누락·판단 근거·검토 이력 |

소득 계약은 금액·KRW, 개인/가구, 월/연, 세전/세후, income/assessed_income(소득인정액)/unknown, 적용연도·가구원 수, 기준중위소득 비율을 분리한다. 이 도구는 소득 비교 evaluator가 아니며 의미·단위 변환이나 자격 추론을 하지 않는다. 모르면 unknown/null을 유지한다.

구조 검사는 extra 필드, ID 중복, snapshot/version 불일치, 다른 프로그램 evidence, span 길이 불일치, 미검증 조건의 충족/불충족 단정, 근거 없는 known availability, reviewed 관련 라벨의 근거 누락을 거부한다. **실제 정책 사실성·주석 정답·live raw 존재를 인증하지 않는다.** policy_annotation은 live namespace와 DB raw 참조를 요구하며 실제 실험 전에 사람이 원문을 복원·검토한다. 실DB evidence resolver는 후속이다.

`tests/fixtures/evaluation/synthetic.json`의 두 case는 **합성 계약 테스트 전용**이다. synthetic_contract, fixture namespace, `[합성]` 질문, `synthetic:` IDs와 합성 notice를 요구한다. raw_artifacts는 기존 합성 Gov24 fixture를 가리킨다. CLI는 로컬 bytes SHA·pointer·발췌 span을 검증하지만 실제 정책 라벨/검토 완료/성능 데이터로 승격하지 않는다. 모든 실제 품질 실험에서 synthetic_contract를 제외한다.

```powershell
uv run --locked benefit-rag validate-evaluation --file tests/fixtures/evaluation/synthetic.json
```

## 네 retrieval baseline

검색 엔진은 아직 미구현이다. corpus·질의·confirmed_profile·평가일·라벨·공통 정책·program budget을 고정하고 검색 방법만 바꾼다.

| 방법 | 정확한 계약 |
|---|---|
| SQL-only | source/namespace/current version 뒤 검증된 정책 metadata와 사용자 확인 age/residence/employment/household/income 맥락만 처리. 정책 필드 미검증이면 predicate 비활성. null/unknown·OR/예외 미해결은 유지. 소득 의미·단위·연도/가구가 안 맞으면 비교 금지 |
| BM25-only | 같은 후보 범위 내 고정 Kiwi/token 정책 + BM25Okapi. 원질문 사용, rewrite 없음 |
| Vector-only | 같은 후보 범위 내 고정 BGE-M3 revision/dense representation + cosine. learned sparse는 BM25 아님 |
| Hybrid | 동일 BM25+Vector 목록을 RRF(제안 rrf_k=60) 결합. raw score 합산 없음 |

현재는 검증된 구조화 정책 조건이 없어 SQL profile predicate를 활성화할 수 없다. usable constraint가 없는 질의는 공통 후보 전체를 반환한다. **관련성 rank가 없는 SQL set은 source, source_record_id, program_id 오름차순**으로 정렬하고 budget으로 자른다. 이를 semantic score로 포장하지 않으며 임의 SQL LIKE 검색을 SQL-only에 섞지 않는다. BM25/vector 동점도 안정적 ID 정렬을 쓴다.

candidate_top_k=40/retriever, program 평가 budget=20, K=5/10/20은 시작 제안이다. current program version으로 묶어 program 중복을 제거한다. SQL은 위 결정 순서를 쓴다. UI final_result_limit=5와 설명 근거 program당 최대 2개·전체 10개/6,000 tokens 제안은 별도 설정이다.

### A. 통제된 retrieval 비교

동일 snapshot·질의/confirmed profiles·라벨·budget·공통 후보 정책·집계를 사용한다. namespace/current-version은 공통 적용한다. validated condition handling을 켠다면 같은 predicate와 unknown 유지 정책을 네 방법에 적용하고 manifest에 기록한다. 필터/청킹/model/rewrite를 함께 바꾼 결과를 hybrid 개선이라고 부르지 않는다.

관련성과 자격/신청 상태는 별개다. relevant_program_ids 분모를 filter가 제거한 후 몰래 줄이지 않는다. 정당한 조건/회차 제외로 topical recall이 달라질 수 있으므로 제거 사유·조건 결과를 같이 보고한다. 현재 actionable 안내에서는 downstream에서 confirmed closed **회차**를 제외하고 unknown은 경고와 함께 유지한다. 작년 회차 마감≠프로그램 폐지이며 과거 안내 질문은 별도 시나리오다.

### B. filtering ablation

retriever·representation·candidate/result budget을 고정하고 validated metadata/eligibility handling만 끄고 켠다. 각 제거 candidate의 조건·근거·원래 관련/유지 필요 라벨·삼값 결과를 기록한다. OR 일부 실패·unknown·비호환 소득·unvalidated code로 제거하면 false exclusion이다. 확인된 완전 불충족/closed 회차 제외 자체는 오류가 아니며 review label로 구분한다. retrieval 방법 비교와 별도 표로 보고한다.

## 지표와 분모

주요 retrieval 수치는 **reviewed + complete + 관련 프로그램 1개 이상** 질의에 계산한다. draft/unlabeled는 제외한다. partial은 알려진 positive 진단값만 별도 보고하고 주평균에 섞지 않는다. R은 program ID 단위 정답 집합이며 frozen version으로 근거를 복원한다. 여러 chunk는 같은 program을 한 번만 센다.

| 지표 | 분모·정의 / 불완전 라벨·무응답 처리 |
|---|---|
| Program Hit Rate@K | eligible 질의 중 topK가 R 하나 이상을 포함한 비율. positive 질의의 검색 무응답은 0 |
| Program Recall@K | 질의별 `intersection_count / relevant_count`의 macro mean. 질의 수·R 크기 보고. R=빈집합은 별도, partial은 진단값만 |
| MRR | 첫 관련 program rank 역수의 macro mean. budget=20 안에 없거나 검색 무응답이면 0. R 빈 질의 제외 |
| Citation Accuracy | 검토할 claim-citation 연결 중 frozen 근거가 실제 claim을 지원하는 연결 비율. fabricated/unrecoverable citation은 실패. 인용 0이면 N/A와 count=0, citation-required claim 누락률 별도 |
| Answer Correctness | reviewed expected facts 중 정확히 전달한 비율 + 허위/금지 주장 수. fact 분모·case macro mean 보고. 필수 fact 있는 무응답은 누락/0. 기대 기권 case는 별도. partial fact 라벨 별도 |
| Faithfulness | 생성한 검증 가능 factual claim 중 제공 evidence가 지원한 비율. 근거 없는 claim 실패. factual claim 0이면 N/A; 무응답률·필수 사실 coverage를 같이 보고해 기권 점수 편향 방지 |
| 삼값 자격 오류 | reviewed expected findings 분모의 confusion matrix, false satisfied/false not_satisfied 및 expected needs_confirmation의 단정률. 판정 누락 별도 오류, 공식 심사 정확도 아님 |
| False exclusion | review상 유지해야 할 program-query 쌍 중 filter가 잘못 제거한 수/유지해야 할 수. 개수·사유·근거 보고. 분모 0은 N/A, 미검토/partial 별도 |
| No-result/insufficient evidence | reviewed complete R 빈집합 또는 기대 확인 요청 case 별도. 미채움·기권/확인 안내·허위 positive 수 보고. positive 라벨을 발명해 Recall에 넣지 않음 |
| Latency/API usage | 실제 실행 case 수와 오류/timeout 수 명시. 단계별 p50/p95, cold/warm·장비·batch·RAM/VRAM·request/input/output token 수. 오류를 제외하고 숨기지 않음 |

지원 조건 aggregate/예외 의미가 미확인이라면 기대 결과도 확인 필요다. 원문·연도·가구·소득 산정이 맞지 않는 비교를 정답으로 만들지 않는다. metric 계산기와 실제 검색 실험은 이번 구현 밖이다.

## 답변 생성 비교

retrieval-only 결과부터 보고한다. 생성 비교는 **gpt-4o-mini, endpoint, prompt version, evidence budget, decoding 설정**을 고정하고 계정 권한을 먼저 확인한다. 한 요소를 바꾸면 별도 실험이다. untrusted retrieved text/prompt injection, 공식 상세 링크 상태, freshness, 확인 필요 표현을 검토한다. 유료 호출 예산/승인과 실근거가 없어 이번에는 생성하지 않는다.

필수 시나리오는 프로필 결측, 소득 정의/단위 충돌, AND/OR/예외 복합 조건, 애매한 정책 문장, 적용연도 불일치, 만 나이 경계, 종료된 접수 기간, 상시 표기, 시행일과 신청일 혼동, 근거 부족, 결과 없음, 문서 내부 prompt injection이다. “관련” 라벨과 “검증된 조건 충족” 라벨을 분리한다.

조건 단위 삼값 혼동행렬, false satisfied/false not_satisfied, 필요한 상황에서 needs_confirmation을 유지한 비율을 보고한다. 정책의 미표현 조건과 공식 심사 결과는 추정하지 않는다. missing을 false로 바꾸거나 OR의 실패 하나를 전체 탈락으로 만드는 오류는 별도 중대 오류로 기록한다.

생성 평가에는 claim→document/raw pointer 존재, 인용된 문장의 실제 뒷받침 여부, 허용 공식 URL 일치, 정책 금액/기간/절차 생성 여부, 데이터 갱신 시점 표시, 미확인 조건 누락 여부를 포함한다. 참조 ID가 존재해도 claim을 지원하지 않으면 citation correctness 실패다. 자동 검사와 사람의 근거 검토를 함께 사용한다.

동일 장비에서 cold/warm, corpus/index 크기, 질문 길이, batch/최대 토큰 수를 고정하고 수집·tokenization·embedding·검색·fusion·생성을 각각 측정한다. latency p50/p95, peak RAM/VRAM, API request/token 수, 캐시 적중 및 당시 과금 기준을 기록한다. 소량 샘플과 분포를 공개하고 유료/대형 모델 실험은 명시적으로 실행한다. reranker는 recall이 확보된 상태에서 ranking 오류가 남고 추가 latency/비용을 감수할 근거가 있을 때만 도입한다.
