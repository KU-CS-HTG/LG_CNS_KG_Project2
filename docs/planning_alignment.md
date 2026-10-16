# 사전기획서 및 피드백 정렬 기록

기준일 2026-10-16 (Asia/Seoul). 시작 branch `seok-kim`, commit `c17cd68deb6ce3d9df642e3bb793ff321a3957fd`, 작업 트리 clean. 기획서는 확정 기술명세가 아니며 이번 사용자 피드백을 정렬 기준으로 사용했다. 커밋·push·브랜치 전환·DOCX 수정·에이전트 위임은 하지 않는다.

## 읽은 자료

`C:/Users/Admin/Downloads/RAG_프로젝트_사전기획서_양식 (2).docx`를 ZIP/XML로 읽었다. SHA256 `9ad2f4f617e0972ad6506f64692c98a3d62f14b441df6ebf960f1c2af798df79`. Desktop의 `RAG_Project_Resource` 폴더에도 같은 이름이 있으며 SHA256은 `d835945f2927d70e960f9e1966744b527a4a20e36de9d1aa198ae703d1e52c56`이다. 두 파일의 364개 문단을 비교했으며 작성자 칸 외 기획 내용은 같았다. 작성일 칸은 비어 있어 파일 수정 시각을 발표일로 대체하지 않았다. 원본은 저장소에 복사하거나 수정하지 않았다.

## 정렬과 합격 기준

| 현재 방향 | 기존 구현 / 차이 | 이번 변경 | 상태·근거 | 합격 기준 |
|---|---|---|---|---|
| 개인 맞춤형 정부 지원제도 탐색 및 신청 안내 | 문서 첫 표현이 청년 중심으로 읽힐 수 있음 | 제품은 개인 전체, coverage는 확보·검증 데이터, 초기 검증 cohort는 청년 구직자·신혼부부·영유아 양육 가구로 분리 | confirmed_direction: DOCX 3·5장 및 사용자 피드백 | README/architecture/AGENTS에 세 범위 구분, 기업 지원 제외 |
| 근거가 충분하면 3–5개 안내 | 개수·출력 계약 미정 | 최대 5개, 부족하면 0–2개, 전체 목록 주장 금지 | confirmed_direction | candidate_top_k·근거 예산·최종 표시 수 분리 |
| 보조금24 우선 | 목록 adapter/raw-first 구현, 상세/조건 미구현 | 기존 경로 보존, endpoint 계약과 상세 덮어쓰기 방지 설계 명시 | implemented / verified_in_this_run: 명세 비교·회귀 테스트 | live 인증을 fixture와 혼동하지 않음 |
| 실제 데이터 조사 | 결측·길이 재현 도구 없음 | `profile --run-id --dataset-kind` | implemented; 검사 결과는 verification 최신 절 | namespace 혼합 거부, 분모·빈 표본·partial 표시, 읽기 전용 |
| SQL와 RAG 역할 분리 | 삼값 연령/AND/OR 구현, 검색·생성 없음 | 정형 검증은 deterministic, 자연어 근거 검색·설명은 RAG | confirmed_direction; 검색 이점은 hypothesis_to_test | 수치/예외를 의미검색으로 자격 확정하지 않음 |
| 불완전한 Top-K/청킹 선택 | 필드 문서 unique(version,field)만 존재 | architecture 결정표·별도 retrieval representation·후속 child chunk 설계 | provisional_choice | 적용된 초기 migration과 원문 offsets 보존 |
| 비교 평가 | 세 검색 방식 개요, SQL/데이터 계약 없음 | 네 baseline·통제 비교·filter ablation·평가 계약·합성 예제 검증 | implemented(계약 도구) / provisional_choice(실험) | 정답과 자격 라벨 분리, split 누출 검사, 측정치 미생성 |
| 신청 가능한 회차 안내 | 현재 availability unknown | 확인된 closed 회차만 actionable 결과 제외; unknown 경고 유지 | confirmed_direction; evaluator는 후속 | 회차 마감≠프로그램 폐지, 시행일≠접수일 |

`verified_in_this_run`은 본 실행 관측·검사에만 사용한다. `implemented`는 검색 품질이나 live 성공을 뜻하지 않는다. 수집 schedule, 모델 revision, 실제 corpus 규모·라벨은 별도 상태로 관리한다.

## 외부 주장 검증 대장

모든 확인일은 2026-10-16. 역사적 수치를 README·요구사항·데모·성능 결론으로 자동 전파하지 않는다.

| 주장 | 원출처 URL / 발표·기준일 | 적용 모집단·범위 | 상태 및 제한 |
|---|---|---|---|
| 약 1만 개 프로그램 | [행안부의 KTV 안내](https://www.mois.go.kr/video/bbs/type019/commonSelectBoardArticle.do?bbsId=BBSMSTR_000000000255&nttId=97400&searchCode1=A06), 2022-12-16 | 당시 보조금24 안내 규모 | verified_in_this_run: 역사적 안내 문구. 현재 API totalCount·개인 corpus·본 프로젝트 coverage는 blocked_or_unknown. 개발 트래픽 10,000과 프로그램 수는 별개 |
| API 업데이트 실시간 | [공공데이터포털](https://www.data.go.kr/data/15113968/openapi.do), 메타데이터 수정일 2025-12-29 | 데이터셋 공시 주기 | verified_in_this_run: 표시 확인. 변경 반영 지연·개별 정책 현행성 SLA는 blocked_or_unknown |
| 하루 한 번 수집 | DOCX 7장, 발표일 미기재, 외부 URL 해당 없음 | 프로젝트 schedule 제안 | provisional_choice: 자동 배치 미구현. API 주기와 분리하고 계정 한도·변경률 확인 후 결정 |
| 522명 조사, 57.3% 지원 필요, 77.4% 긴급지원 미경험, 36.0% 도움 요청 방법 문제 | [서울신문 조사 보도](https://www.seoul.co.kr/news/plan/2021/10/05/20211005009010), 입력 2021-10-04 / 수정·지면 2021-10-05, 조사 2021-09-07~12 | **가구소득 월 400만원 미만 성인 522명** | verified_in_this_run: 보도의 수치·표본 확인. 개인 월소득으로 바꾸지 않음. 36.0%는 복지사각 원인 응답이며 미수령자만의 미신청 사유로 단정하지 않음. 원설문·표집/가중치 미검증; 현재 전체 국민/제품 cohort 일반화 금지 |
| 약 205만 명·20%, 상반기 미감면액 약 1,673억원 | [황정아 의원 보도자료](https://theminjoo.kr/main/sub/news/view.php?brd=17&post=1213782&search=&sno=0), 2025-10-01; 기준 2025년 1–6월 | 통신비 감면대상 10,238,384명 중 미감면 2,049,311명 | verified_in_this_run: 의원실 발표와 표 확인. 금액은 평균 감면액을 이용한 **추정**이며 모든 정부 혜택 미수령액이 아님. 과기정통부 제출 원자료·산식의 독립 검증은 blocked_or_unknown; 표의 단위/산식 표기도 재확인 필요 |
| 경쟁 서비스 자연어 지원 한계·일반 LLM 최신정보 부재 | DOCX 4장에 현재 기능 원출처 URL·검증일 없음 | 제품 버전·계정·기능·시점 미지정 | hypothesis_to_test / blocked_or_unknown: 현재 기능 직접 검증 안 함. 일괄적 기능 부재·우위 주장 금지. 동일 과제와 공식 기능 문서로 후속 검토 |
| 목록/상세/조건 필드·인증 | [공식 Swagger](https://infuser.odcloud.kr/api/stages/44436/api-docs?1684891964110), 명세 게시일 미확인 | 문서의 v3 계약만 | verified_in_this_run: 저장된 paths/definitions와 동일, SHA256 `4666822260124d3b6f18decfaf82c07184e54a51a7ce750c1a631e0e049769ba`. 실제 타입·코드 값 의미·계정 접근은 blocked_or_unknown |
| Hybrid/RAG가 생활어·표현 차이·정확한 행정 용어·설명을 개선 | DOCX 6·8장, 외부 성능 근거 없음 | 본 프로젝트 고정 corpus/질의 | hypothesis_to_test: 네 baseline과 고정 생성 조건으로 검증. SQL/BM25가 충분한 결과도 유효 |

인증키는 현재 환경에 없고 `.env`도 없다. 이번 정책 API 호출·실제 라벨·성능 점수는 없다. 원출처가 부족한 주장은 미확인으로 유지하며 추정·제안을 확정 사실로 승격하지 않는다.
