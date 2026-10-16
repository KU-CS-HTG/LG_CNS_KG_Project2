# 공식 데이터 조사와 접근 상태

확인 기준일: **2026-10-16, Asia/Seoul**. 공식 포털 HTML과 그 페이지가 제공한 Swagger를 다운로드해 확인했다. 웹 열람 도구의 포털 상세 페이지 요청은 Internal Error였으나, 승인받은 로컬 HTTPS 요청은 세 페이지 모두 HTTP 200이었다. 이는 **문서 접근 성공**이며 정책 데이터 API 실접속 성공이 아니다.

## 공통 접근 상태

`GOV24_SERVICE_KEY`, `DATA_GO_KR_SERVICE_KEY`, `BOKJIRO_SERVICE_KEY`가 현재 프로세스에 없다. 활용신청 승인 여부와 계정별 실제 할당량은 확인할 수 없다. `.env` 같은 비밀 파일의 내용을 조사하지 않았다. 키 없이 인증 API에 불필요한 호출을 하지 않았으며 정책 응답 표본은 0건이다. 유료 생성·대량 수집·정책 페이지 스크래핑도 하지 않았다.

세 데이터셋 모두 공식 페이지에서 무료, 이용허락범위 제한 없음, 개발/운영 자동승인, 업데이트 주기 실시간으로 표시된다. 자동승인은 신청 자체가 필요 없다는 의미가 아니다. 포털 로그인 → 해당 데이터 활용신청 → 승인 상태 및 발급 키 확인이 필요하다. 포털의 실시간 표시는 개별 정책의 현행성이나 실시간 자격 결정 보장이 아니다. 재배포 시 출처·수집일·변경 여부를 함께 표기하는 것을 프로젝트 규칙으로 한다.

공식 페이지의 개발계정 신청 가능 트래픽 값과 일반 오류 설명의 일일 호출 한도는 확인했으나, 계정별 활성 할당량·초당 제한·초기화 시각은 미확인이다. 표의 숫자를 초당 처리량으로 해석하지 않는다. 구현의 3회 재시도·30초 대기 상한·페이지당 100건 상한은 **프로젝트의 보수적 제한**이며 제공기관 한도가 아니다.

## 보조금24 / 정부24

| 항목 | 확인 내용 |
|---|---|
| 공식 데이터셋 | **행정안전부_대한민국 공공서비스(혜택) 정보** |
| 제공기관 | 행정안전부 / 정부포털운영팀 |
| 공식 문서 | [공공데이터포털 15113968](https://www.data.go.kr/data/15113968/openapi.do) |
| 포털 연결 Swagger | [공식 명세](https://infuser.odcloud.kr/api/stages/44436/api-docs?1684891964110) |
| 인증 | `serviceKey` query 또는 `Authorization` header의 API key 방식. 구현은 Decoding 키를 HTTPX query로 한 번 인코딩 |
| base | `https://api.odcloud.kr/api` |
| 목록 | `GET /gov24/v3/serviceList` |
| 상세 | `GET /gov24/v3/serviceDetail`, `cond[서비스ID::EQ]` 필터 지원 |
| 지원조건 | `GET /gov24/v3/supportConditions`, 동일 서비스ID 필터 지원 |
| 파라미터 | 공통 `page` 기본 1, `perPage` 기본 10, `returnType`; JSON 기본, XML 선택 문구 있음. 인증 필수. 페이지 최대치는 확보한 Swagger에 명시되지 않음 |
| 응답 | `page`, `perPage`, `totalCount`, `currentCount`, `matchCount`, `data[]` |
| 개발계정 트래픽 | 포털 표시 10,000. 운영은 활용사례 등록 후 증설 신청 가능 |
| 범위 | 중앙부처·지자체·공공기관·교육청의 공공서비스. 개인 외 대상도 포함하므로 추천 대상 선별이 후속 필수 |
| 구현/검증 | 목록의 fixture 수집·정규화·저장 구현. live HTTP 어댑터는 MockTransport만 검증. 상세·지원조건 연동은 미구현 |

목록 명세 필드: `서비스ID`, `서비스명`, `지원유형`, `서비스목적요약`, `지원대상`, `선정기준`, `지원내용`, `신청방법`, `신청기한`, `상세조회URL`, `소관기관코드`, `소관기관명`, `부서명`, `조회수`, `소관기관유형`, `사용자구분`, `서비스분야`, `접수기관`, `전화문의`, `등록일시`, `수정일시`. `조회수`는 integer, 나머지는 string으로 선언되어 있다. 필수/nullable의 실제 운영 의미는 아직 검증하지 못했다.

상세는 `서비스목적`, `구비서류`, `온라인신청사이트URL`, `접수기관명`, `문의처`, `행정규칙`, `자치법규`, `법령` 등 추가 필드를 명시한다. 목록의 목적 요약·상세조회URL과 상세의 목적·온라인신청사이트URL은 다른 의미다. 섞어서 덮어쓰지 않는다.

지원조건에는 `JA0110`/`JA0111` 대상연령 시작/종료, `JA0201`~`JA0205` 중위소득 구간, 성별·가구·취업·기업 관련 코드가 있다. 코드 문자열의 값 집합, 빈값의 의미, AND/OR 및 예외 관계는 확보한 명세만으로 확정할 수 없다. **코드가 없다고 자격 없음으로 처리하거나 소득 구간을 현행 소득 기준으로 사용하지 않는다.** 전체 명세 필드는 [source_specs.json](source_specs.json)에 보존했다.

정부24와 보조금24를 별개 데이터셋으로 중복 수집하지 않는다. 정부24는 서비스 플랫폼, 보조금24는 혜택 안내 맥락이며 이번 우선 통합 후보는 위 데이터셋 하나다. 정부24의 [공식 안내](https://www.gov.kr/portal/ntcItm/83584)도 공공데이터포털 API 제공을 안내한다. 과거 안내의 건수는 현재 수집량이나 완전성 근거로 사용하지 않는다.

## 복지로 중앙부처

| 항목 | 확인 내용 |
|---|---|
| 공식 데이터셋/제공기관 | **한국사회보장정보원_중앙부처복지서비스** / 한국사회보장정보원, 포털운영부 |
| 문서 | [공공데이터포털 15090532](https://www.data.go.kr/data/15090532/openapi.do), 페이지 내 embedded Swagger |
| base | `https://apis.data.go.kr/B554287/NationalWelfareInformationsV001` |
| 목록 | `/NationalWelfarelistV001` |
| 목록 필수 query | `serviceKey`, `callTp=L`, `pageNo`, `numOfRows`, `srchKeyCode` (001 제목, 002 내용, 003 제목+내용) |
| 선택 query | `searchWrd`, `lifeArray`, `trgterIndvdlArray`, `intrsThemaArray`, `age`, `onapPsbltYn`, `orderBy` |
| 상세 | `/NationalWelfaredetailedV001`: `serviceKey`, `callTp=D`, `servId` 필수 |
| 페이지 | `pageNo` 기본 1·최대 1000, `numOfRows` 기본 10·최대 500이라는 명세 설명 |
| 형식/구조 | XML. 명세의 `wantedList`에 `totalCount`, `pageNo`, `numOfRows`, `servList`; 상세 `wantedDtl` |
| 내용 | 목록 `servId`, `servNm`, `jurMnofNm`, `jurOrgNm`, `servDgst`, `servDtlLink`, `svcfrstRegTs` 등. 상세 `tgtrDtlCn`, `slctCritCn`, `alwServCn`, `crtrYr`, `applmetList`, 문의처/웹사이트/법령/서식 목록 |
| 트래픽 | 포털 개발계정 표시 **100**. 보조금24 한도를 적용하지 않음 |
| 범위/제한 | 사회보장조정과가 대상으로 선정한 중앙부처 사업. 모든 정부 혜택의 완전한 목록이 아님 |
| 상태 | 공식 문서 확인. 실제 XML·오류 코드·반복 노드 형태 미확인. 구현하지 않음 |

포털에는 `활용가이드_중앙부처복지서비스(v2.2).doc`가 연결되어 있다. 이번에는 페이지의 Swagger를 확인했고 DOC 첨부 본문은 읽지 않았다. Swagger 추가 메타데이터에는 `www.bokjiro.go.kr/ssis-tbu/...V001.do` URL도 나타난다. base host와 다른 이 URL을 검증된 대체 API endpoint로 취급하지 않는다. 실제 승인 키로 어느 경로가 동작하는지 확인해야 한다.

## 복지로 지자체

| 항목 | 확인 내용 |
|---|---|
| 공식 데이터셋/제공기관 | **한국사회보장정보원_지자체복지서비스** / 한국사회보장정보원, 포털운영부 |
| 문서 | [공공데이터포털 15108347](https://www.data.go.kr/data/15108347/openapi.do), 페이지 내 embedded Swagger |
| base | `https://apis.data.go.kr/B554287/LocalGovernmentWelfareInformations` |
| 목록 | `/LcgvWelfarelist`: `serviceKey` 필수; `pageNo`, `numOfRows` 선택 |
| 추가 선택 query | `lifeArray`, `trgterIndvdlArray`, `intrsThemaArray`, `age`, `ctpvNm`, `sggNm`, `srchKeyCode`, `searchWrd`, `arrgOrd` |
| 상세 | `/LcgvWelfaredetailed`: `serviceKey`, `servId` 필수 |
| 응답 | XML `response`, `resultCode`, `resultMessage`, `totalCount`, `pageNo`, `numOfRows`, `servList` |
| 내용 | 목록 `servId`, `servNm`, `ctpvNm`, `sggNm`, `bizChrDeptNm`, `servDgst`, `servDtlLink`, `lastModYmd` 등. 상세 `sprtTrgtCn`, `slctCritCn`, `alwServCn`, `aplyMtdCn`, `enfcBgngYmd`, `enfcEndYmd`, 문의처·법령 등 nested 목록 |
| 트래픽/페이지 상한 | 개발계정 표시 **1,000**. 확보 명세에서 페이지 크기 최대치 미확인 |
| 범위/제한 | 지자체 복지로 등록 서비스. 시도·시군구 명칭의 실제 결측과 전국/지역 혼합 의미 미확인 |
| 상태 | 문서만 확인, 구현·실접속 미검증 |

지자체 명세의 `enfcBgngYmd`/`enfcEndYmd`는 **시행일자**이며 신청 접수 기간으로 변환하지 않는다. 중앙·지자체 Swagger의 여러 목록은 `object`로 선언되어 있다. 실제 XML에서 반복되는 형식은 표본으로 검증해야 하며 단일 object라고 단정하지 않는다. 두 API의 필드명은 유사하더라도 서로 다른 어댑터 계약을 둔다.

## 표본과 다음 검증

공식 Swagger에는 이 구현에 바로 사용할 완전한 정책 응답 예제가 없었다. 따라서 `tests/fixtures/gov24/`는 명세의 envelope/필드 타입을 참고해 직접 만든 **합성 3건·2페이지**다. 실제 지원금·기간·기관·URL을 넣지 않았다. 한 null 필드, 빈 문자열, 빠진 필드, 알 수 없는 nested 추가 필드를 포함한다. 이 변이는 방어 로직 검증용이며 실제 결측률·지역 분포·중복률에 대한 관측 결과가 아니다.

live 검증의 선행 조건은 승인 키의 안전한 공급이다. 1페이지 3건부터 시작해 raw를 저장하고 필드/타입/날짜/빈값/서비스ID 중복을 조사한다. 상세와 지원조건 각각 소량을 추가 확인해 지원대상·선정기준·신청 정보 및 URL 상태를 검사한다. 문서와 응답 차이는 요청 시각·endpoint·raw ID로 기록한다. 현재는 비교 가능한 실응답이 없어 **차이 없음이 아니라 차이 미확인**이다. 이후에만 파서·스키마를 확정하고 개인/카테고리 범위와 갱신 계획을 정한다.
