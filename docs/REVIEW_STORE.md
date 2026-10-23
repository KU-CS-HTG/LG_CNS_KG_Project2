# 검수 저장소 (`src/review/`, B 담당)

질문별로 "이 제도가 정답인가"를 사람이 판정하고, 답변에 대한 메모를 남기고, 그것을 평가·검색·생성에 반영하기 위한 저장소다.
앱의 **검수 모드**(C)는 `ReviewStore`의 `add_*` 메서드로 쓰고, 평가는 `export` 결과를 읽는다. 설계 합의 내용은 아래 "합의된 결정"을 따른다.

## 합의된 결정
| # | 결정 |
|---|---|
| 1 | 저장 위치는 git의 `reviews/`, **검수자마다 파일 하나**(`reviews/events/<이름>.jsonl`)라 병합 충돌이 없다 |
| 2 | 판정 4단계 `good`(2점) / `partial`(1점) / `bad`(0점) / `unsure`(점수 제외) |
| 3 | `bad`·`partial` 사유 8개(`not_eligible` `wrong_region` `expired` `irrelevant` `duplicate` `generic_noise` `info_wrong` `other`), `unsure` 사유 3개(`needs_source_check` `needs_discussion` `ambiguous_question`) |
| 4 | 판정이 충돌하면 조정자(`reviews/reviewers.json`의 `adjudicators`, 기본 `taegyu` = B)가 정한다 |
| 5 | dev/test 분리 8:2 (질문 ID 해시로 정해 재현 가능). test는 튜닝에 쓰지 않고, 내보낸 test 시험지는 덮어쓰지 않는다. **기존 36문항은 이미 튜닝에 썼으므로 모두 dev** |
| 6 | 판정의 버전 해시는 서비스명·지원대상·선정기준·지원내용·신청방법·**신청기한**. 기한이 지난 제도(`expired`)는 판정과 별개로 내보내기·대기열에서 **자동 제외**(판정 기록은 보존) |
| 7 | 제도별 주석(`service_note`)은 "검수자 메모(날짜)"로 구분해 생성기에 제공할 수 있다. 출처 URL·확인일·만료일 필수 |
| 8 | 실사용 질문은 쓰지 않는다. 사용하는 질문은 모두 동의된 것으로 보고 `source`는 `team`/`synthetic`/`paraphrase`만 허용 |

## 파일 구조
```
reviews/
  events/<검수자>.jsonl    # 모든 이벤트 (질문·판정·실행·메모·주석·지침), 추가만 한다
  reviewers.json           # {"adjudicators": ["taegyu"]}
  exports/                 # 내보낸 시험지 (예: dev_v1.jsonl, test_v1.jsonl)
```
`reviews/events/` 파일 이름은 이벤트의 `reviewer`와 같아야 한다(다르면 오류). 파일은 직접 편집하지 말고 `ReviewStore`나 CLI로만 쓴다.

## 이벤트 공통 형식
```json
{"event_id": "uuid", "client_event_id": "선택", "type": "judgment", "ts": "2026-10-23T09:12:00Z",
 "reviewer": "taegyu", "schema": 1, "supersedes": null, "data": {}}
```
- 수정은 새 이벤트가 이전 것을 `supersedes`로 대체하는 방식이다. 같은 (질문, 제도, 검수자)의 판정은 가장 최근 것이 이긴다.
- `client_event_id`가 같은 이벤트는 한 번만 저장한다 (Streamlit이 화면을 다시 실행해도 중복되지 않게).

## 이벤트 종류와 필드
| type | 주요 필드 |
|---|---|
| `question` | `text`, `source`, `split`(생략하면 8:2 해시), `gold_filters`(age·region·field·situation·today), `tags`, `category`, `topic`, `expected_answer`, `legacy_id`, `paraphrase_of`. `question_id`는 정규화한 문장의 해시(`q_xxxxxxxx`) |
| `question_status` | `question_id`, `status`(`active` `retired` `no_answer` `out_of_scope`). `no_answer`는 독립 검색 3개 이상의 `checks`가 필요 |
| `run` | `run_id`, `question_id`, `system`(git·index·설정), `candidates`[`service_id` `rank` `source`(`hybrid_filter` `vector` `bm25` `independent_search`) `score` `service_version`], `answer`{`text` `evidence_ids`}, `shuffled`, `k_shown` |
| `judgment` | `question_id`, `service_id`, `service_version`, `label`, `reason`, `note`(500자), `provenance`{`by`: `human`\|`claude`}, `run_id` |
| `answer_memo` | `run_id`, `kind`(`omission` `error` `style` `rule`), `text`, `scope`, `severity`, `status`(`open` → `accepted`/`rejected` → `promoted`+`guideline_id`) |
| `service_note` | `service_id`, `service_version`, `kind`, `text`, `source_url`, `verified_at`, `expires_at`, `status` |
| `guideline` | `guideline_id`, `text`, `status`(`proposed` `active` `retired`), `applies_to`, `from_memos`, `approved_by`(active는 필수) |

## 합의 규칙 (`State.consensus`)
1. 제도 내용이 바뀐(버전이 다른) 판정은 `stale`로 따로 두고 합의에 쓰지 않는다.
2. 사람(`human`) 판정이 있으면 `claude` 1차 판정은 무시한다.
3. `unsure`는 합의에서 빠진다. 다른 사람이 정했으면 그것을 따르고, 전부 `unsure`면 `unsure`.
4. 조정자의 판정이 있으면 그것이 이긴다(`adjudicated`). 없고 서로 다르면 `conflict`, 같으면 `consensus`.

## 내보내기와 대기열
- `export`는 질문마다 `relevant_service_ids`(good만, 개수 상한 없음), `relevance`(good 2·partial 1), `hard_negatives`(bad), `unsure`, `conflicts`, `stale`, `judged_coverage_at_10`, `label_sources`를 담은 jsonl을 만든다. `id`·`category`·`question`·`filters`·`expect_no_answer`는 기존 `run_eval`이 그대로 읽는다.
- 기한이 지난 제도, 활성 데이터에 없는 제도는 제외하고 보고서에 건수를 남긴다. 기한을 해석할 수 없으면(상시·접수기관 별 상이) 지난 것으로 보지 않는다.
- 대기열은 질문마다 여러 검색 방식(`hybrid_filter` `vector` `bm25`)의 상위 n개를 합쳐 **판정이 없는 새 후보(new)** 와 **제도 내용이 바뀐 후보(changed)** 를 보여 준다. `unsure`와 `conflict`는 별도 목록이다.

## 명령
```powershell
python -m src.review import-legacy            # eval/questions.jsonl의 질문·정답을 이벤트로 이전 (다시 실행해도 중복 없음, 기존 질문은 모두 dev)
python -m src.review import-questions q.csv --reviewer taegyu [--dry-run]   # 새 질문 일괄 등록 (csv/jsonl)
python -m src.review validate                 # 모든 이벤트 파일 검증 (오류가 있으면 종료 코드 1)
python -m src.review stats                    # 질문·판정·사유 통계, 열린 메모, 활성 지침
python -m src.review agreement taegyu minkyu  # 두 검수자의 일치율과 kappa
python -m src.review export --split dev --out reviews/exports/dev_v1.jsonl --today 2026-10-23
python -m src.review queue --n 30             # 검수 대기열 (색인 필요)
```
`--db`는 A의 SQLite(`data/services.db`)이고 읽기 전용으로만 연다.

`import-questions`: 열은 `질문`(필수)·`범주`·`주제`·`나이`·`지역`·`분야`·`상황`·`태그`(`;` 구분)·`기대답변`·`출처`(기본 team)·`split` (영문 열 이름도 가능, 엑셀 csv의 BOM 허용). 나이·지역·분야·상황은 `gold_filters`가 된다. 정규화한 문장이 이미 있으면 건너뛰고(재실행해도 중복 없음), 형식 오류는 행 번호와 함께 모아 보여 주며 나머지는 계속 처리한다(오류가 있으면 종료 코드 1). 먼저 `--dry-run`으로 개수·dev/test 배정을 확인한다.

시험지 평가: `python -m eval.run_eval --questions reviews/exports/dev_v1.jsonl --experiment hybrid_filter`. `relevance`가 있는 시험지는 Hit/Recall/MRR에 더해 Precision@K, nDCG@K(good=2, partial=1), bad_rate@K(상위 K 중 '틀림' 판정 비율), unjudged_rate@K(아직 판정 없는 비율)를 출력한다. unjudged가 높으면 정답 목록이 불완전해 지표가 낮게 나온 것일 수 있으니 `queue`로 먼저 채운다.

## 판정 기준: '좋음'은 어디까지인가
판정은 "이 질문을 한 사람에게 이 제도가 **해당될 가능성이 있는가**"로 한다 (`docs/LABELING.md` §정답 기준과 같다).
- **좋음**: 질문에 나온 상황(나이·지역·목적)에 맞는다. 질문에 없는 소득·가구·재산 같은 조건은 따지지 않는다 — 그것만 확인하면 해당 여부가 정해지고 추가 질문이 필요해도 좋음이다 (확인은 답변 단계의 "확인 필요"가 맡는다).
- **나쁨**: 질문에 이미 나온 정보와 어긋난다. 사유는 `not_eligible`(나이·대상) 또는 `wrong_region`. 추가 질문으로 살아나지 않는다.
- **부분**: 주제는 맞지만 대상·용도가 일부만 겹친다.
- **보류**: 질문이 모호하면 `ambiguous_question`, 제도 원문을 봐야 하면 `needs_source_check`.
- "추가 질문이 필요하다"는 이유만으로 좋음이 되지는 않는다. 질문과 실제로 겹치는 부분이 있어야 하며, 가능성이 아주 낮은 제도까지 넣으면 정답이 넓어져 Precision이 왜곡된다. 검수자끼리 기준이 갈리면 충돌로 잡히므로 팀이 같은 기준을 쓴다.

## 검수 화면 (`app/review_mode.py`)
```powershell
python -m app.review_mode      # 브라우저에서 검수. 색인(Chroma)과 data/services.db 필요
```
왼쪽에 검수자 이름(영문·숫자)을 넣고 dev/test 묶음과 기준일을 고른 뒤 질문을 선택한다. 후보(hybrid_filter·vector·bm25 합집합을 순위 좋은 순으로 `질문당 후보 수`만큼, 왼쪽 슬라이더 3~30, 기본 10)는 질문마다 고정된 순서로 섞여 보이고 어느 검색이 찾았는지는 가린다. 제도마다 좋음/부분/나쁨/보류 + 사유 + 메모를 저장하면 `judgment` 이벤트가, 첫 저장 때 후보 목록이 `run` 이벤트가 남는다. 정답이 없는 질문은 화면 아래 '근거 없음 지정'에서 서로 다른 검색어 3개 이상(한 줄에 하나)을 적어 `no_answer` 상태로 확정한다(후보를 전부 나쁨으로 표시할 필요는 없고, 좋음 판정이 있으면 지정할 수 없다. 해제도 가능). 이미 판정한 제도는 검색에 안 나와도 보이고, 기한이 지난 제도는 숨긴다. 아래쪽 '시스템 답변 검수'는 `generator.answer_question` 결과를 보고 빠진 내용·틀린 내용 메모(`answer_memo`)를 남긴다(OpenAI 키 필요). 로직은 `src/review/session.py`(테스트 포함), 화면은 `app/review_mode.py`이며 `app/main.py`는 고치지 않았다. 붙이려면 main에서 `review_mode.render()`를 호출하면 된다.

## 앱(C)에서 쓰는 법
```python
from src.review import ReviewStore, schema
store = ReviewStore()                                        # reviews/
qid = store.add_question("taegyu", "질문 문장", source="team")
run_id = store.add_run("taegyu", qid, [{"service_id": "...", "rank": 1, "source": "hybrid_filter"}], system={"git": "..."}, answer={"text": "..."})
store.add_judgment("taegyu", qid, service_id, schema.service_version(service), "bad", reason="not_eligible", note="노인 전용", run_id=run_id,
                   client_event_id="화면에서 만든 고유 ID")      # st.form 제출 시 한 번만 호출
store.add_memo("taegyu", run_id, "omission", "신청 기한이 본문에 없음")
```
`service_version(service)`는 A의 레코드(`support_target` 등)를 받는다. 입력이 규칙에 맞지 않으면 `ValidationError`(코드 문자열)가 난다.

## 한계
- 검수는 화면에 보인 후보만 판단한다. 검색이 놓친 제도는 판정되지 않으므로, 후보를 넓게 보여 주고 `independent_search`로 직접 찾은 후보를 더한다. `judged_coverage_at_10`으로 범위를 확인한다.
- 정답이 5개를 넘을 수 있으므로 Recall@5는 구조적으로 낮아진다. Precision@K·nDCG@K·bad/unjudged 비율은 `eval/run_eval.py`가 함께 출력한다(위 '명령' 참고).
- 기한 해석은 `eval/label_helper.deadline_status`를 쓴다. A(`parse_deadline`)와 C(`generator._period_expired`)에도 같은 일을 하는 코드가 있어 결과가 다를 수 있다(통합 과제).
