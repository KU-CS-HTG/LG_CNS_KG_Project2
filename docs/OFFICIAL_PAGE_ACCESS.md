# 공식 자료 페이지 접근 (막힐 때 해결 방법)

AI가 '답변 구성'을 채울 때 공식 사이트(고용24·법령정보센터·캠코 등)를 직접 열지 못하면, 검색 결과의 발췌·요약에만 기대게 되어 근거 구절을 원문과 대조할 수 없다.
이 문서는 막히는 원인을 구분하고, **우회하지 않는** 해결 순서를 정리한다. (구현: `src/generate/page_fetch.py`, 화면: `python -m app.amount_mode`)

## 1. 먼저 원인 구분하기

`python -m src.generate.page_fetch check` 를 실행하면 공식 사이트마다 원인이 나온다.

| 증상 | 원인 | 방향 |
|---|---|---|
| `network` (ProxyError·이름 풀이 실패·시간 초과) | 실행 환경의 네트워크 허용 목록 제한 (클라우드 세션) 또는 이 PC의 네트워크 문제 | 허용 도메인 추가 또는 **이 PC에서 직접 실행** |
| `timeout` (ReadTimeout) | 사이트가 느리거나 외부 접속을 늦춤. 한 번은 제한을 두 배로 늘려 자동 재시도함 | `--timeout 60` 으로 다시, 안 되면 본문 붙여넣기 |
| `blocked` (403·429) | 사이트가 자동 접근을 막음 | 우회하지 않는다. 브라우저에서 열어 **본문 붙여넣기** |
| `captcha` (보안문자) | 봇 탐지 | 자동화를 멈추고 사람이 한다 (본문 붙여넣기) |
| `login` | 로그인 필요 | 브라우저에서 로그인해 본문 붙여넣기 |
| `empty` (본문이 비어 있음) | 스크립트로 그려지는 페이지 | `--browser`(Playwright) 또는 본문 붙여넣기 |
| `robots` | robots.txt가 이 경로의 자동 수집을 허용하지 않음 | 읽지 않는다. 본문 붙여넣기 |

## 2. 해결 순서

1. **클라우드 세션이 `network`로 막힌 경우**: 환경 설정(세션 제목 줄의 클라우드 환경 메뉴 → Edit)의 Network access에서 더 넓은 접근 수준을 고르거나, 아래 도메인을 Allowed domains에 넣는다 (Allow package managers 칸은 켜 둔다). 설명: https://code.claude.com/docs/en/cloud-environments#network-access
   `www.law.go.kr, www.moel.go.kr, minwon.moel.go.kr, 1350.moel.go.kr, www.work24.go.kr, m.work24.go.kr, www.gov.kr, www.korea.kr, www.bokjiro.go.kr, www.kamco.or.kr, www.fsc.go.kr, www.kinfa.or.kr, www.kosaf.go.kr, www.kibo.or.kr`
2. **이 PC에서 직접 실행**: 가장 확실하다 (일반 사용자와 같은 네트워크). 같은 명령을 이 PC에서 돌린다.
   - `python -m src.generate.page_fetch fetch URL [URL ...]` (URL 자리에는 실제 주소를 넣는다. `--timeout 60`으로 시간 제한을 늘릴 수 있다) — 읽어서 `data/a_validation/official_pages/`에 저장(HTML·PDF, git에는 안 올라감)
   - `python -m src.generate.page_fetch fetch URL --browser` — 스크립트로 그려지는 페이지 (`pip install playwright && playwright install chromium` 필요, 보통 브라우저로 열어 렌더링만 한다)
3. **본문 붙여넣기**: `python -m app.amount_mode` ② 답변 구성의 "접속이 안 되는 사이트라면 — 본문을 직접 붙여넣기"에 브라우저에서 복사한 본문을 넣으면 주소로 읽은 것과 같은 방식(AI가 값을 찾고 코드가 본문과 대조)으로 채운다. 붙여넣은 본문도 읽은 기록으로 남는다.
4. **근거 구절 대조**: `python -m src.generate.page_fetch verify` — 저장한 답변 구성(AI가 채워 확인 대기 중인 것 포함)의 근거 구절이 그 주소의 본문에 글자 그대로 있는지 확인한다 (✅ 있음 / ❌ 없음 / ⏭ 읽지 못함). 공백·줄바꿈·표 칸 구분선(`|`)은 무시하고, ❌이면 구절의 어디까지 같고 어디부터 다른지와 본문에서 가장 비슷한 곳을 보여 준다(항목 이름을 붙여 다듬어 옮긴 구절은 여기서 걸린다). 이미 읽은 주소는 기록을 쓰고, `--refresh`로 다시 읽는다.
5. 그래도 안 되는 페이지(CAPTCHA 등)는 자동화를 포기하고 사람이 하는 단계로 남긴다.

## 3. 하지 않는 것

- CAPTCHA 풀기, 봇 탐지 회피(위장 헤더, `undetected-chromedriver` 같은 stealth 드라이버, IP 돌려쓰기)는 쓰지 않는다. 사이트가 자동 접근을 원치 않는다는 뜻이므로 공식 API·수동 확인이 맞는 길이다.
- robots.txt가 막은 경로는 읽지 않고, 같은 사이트에는 요청 간격(기본 2초)을 둔다.
- 읽지 못한 페이지를 읽은 것처럼 쓰지 않는다: 근거 구절과 자료 주소는 실제로 읽은 본문·주소만 쓴다.

## 4. 화면 없이 안정적으로 가져오는 방법 (참고)

- 공식 API·공공데이터포털이 있으면 그것을 먼저 쓴다 (이 프로젝트의 보조금24 수집이 그 예다).
- 화면이 쓰는 내부 JSON API가 있으면(F12 → Network) 화면을 긁는 것보다 가볍고 안정적이다. 다만 약관·robots.txt를 먼저 확인한다.
