# 데이터 팩 (팀이 같은 DB·색인을 쓰는 방법)

**문제**: DB(`data/services.db`)와 검색 색인(`data/vectorstore`)은 git에 올라가지 않아서 사람마다 따로 만든 것을 쓴다. 코드가 같아도
색인이 오래됐거나(지역 값 없음 등) 범위가 달라 K-패스가 빠지고 이상한 제도가 섞이는 일이 생겼다.
**해결**: 기준이 되는 한 사람(조장)이 자기 DB·색인을 **데이터 팩(zip)** 으로 묶어 공유 드라이브에 올리고, 팀원은 그것을 내려받아 설치한다.
판정 기준 문서 PDF를 zip으로 나누는 것과 같은 방식이다. 어느 팩이 최신인지는 작은 파일 `data_pack/LATEST.json`을 git으로 공유한다.

```
조장: 데이터 갱신 -> build(팩 만들기) -> zip을 공유 드라이브에 -> LATEST.json 커밋·푸시
팀원: git pull -> status(새 팩 있나?) -> 공유 드라이브에서 zip 받기 -> install -> --check로 확인
```

## 조장이 팩 만들기 (데이터가 바뀔 때마다)
1. **앱·handoff·explain 등 DB·색인을 쓰는 프로그램을 모두 끈다.** (실행 중이면 색인 파일이 일관되지 않을 수 있다)
2. (선택) 팩에 넣기 전 확인: `python -m src.generate.explain --check` (색인 조각 수, K-패스가 색인에 있는지).
3. 팩 만들기:
   ```powershell
   python -m src.datapack build --out "G:\공유드라이브\데이터팩" --by taegyu --notes "지역 보충 적용, 제도 10,695개" --where "공유 드라이브/데이터팩"
   ```
   `--out`은 공유 드라이브 동기화 폴더(또는 만든 뒤 올릴 임시 폴더)다. 끝나면 `data_pack_<시각>.zip`이 생기고 `data_pack/LATEST.json`이 갱신된다.
   DB는 실행 중에도 일관되게 복사(SQLite 백업)하고, 색인 폴더는 그대로 묶는다. 용량은 수백 MB다.
4. zip이 공유 드라이브에 다 올라가면 **LATEST.json을 커밋·푸시**한다 (zip보다 먼저 푸시하면 팀원이 없는 파일을 찾는다):
   ```powershell
   git add data_pack/LATEST.json
   git commit -m "데이터 팩 <id>: <바뀐 점>"
   git push
   ```
   (zip 자체는 git에 올리지 않는다. `--out`을 저장소 밖으로 두면 실수로 올라가지 않는다.)

## 팀원이 설치하기
1. `git pull` (코드와 `LATEST.json`을 받는다)
2. 새 팩이 있는지 확인: `python -m src.datapack status`
   - "새 데이터 팩이 있어요 …" -> 아래로. "최신 데이터 팩을 쓰고 있어요" -> 할 일 없음.
3. 공유 드라이브에서 `status`가 알려 준 zip을 받는다 (동기화 폴더면 이미 있다).
4. **앱·handoff 등을 모두 끈 뒤** 설치:
   ```powershell
   python -m src.datapack install "C:\받은폴더\data_pack_20261027-153012.zip"
   # 또는 폴더만 알려 주기:  $env:DATAPACK_DIR = "G:\공유드라이브\데이터팩"; python -m src.datapack install
   ```
   설치는 ① zip 해시와 파일별 해시를 검증하고 ② 임베딩 모델이 이 컴퓨터 설정과 같은지 보고 ③ 기존 DB·색인을
   `data/a_validation/datapack/_backup_<시각>/`로 옮겨 둔 뒤 ④ 새 것을 넣는다. 중간에 실패하면 기존 데이터를 되돌린다.
5. 확인: `python -m src.generate.explain --check` (조각 수·K-패스가 조장과 같은지). 문제없으면 백업 폴더는 지워도 된다.

## 무엇이 팩에 들어가나
| 들어감 | 안 들어감 |
|---|---|
| `services.db`(SQLite 일관 복사본), `services.db.handoff.json`(DB↔색인 연결 기록), `vectorstore/` 전체(Chroma, BM25 재료, `handoff_manifest.json`) | `data/raw/`(원본 응답), `data/guides/`(PDF, 별도 zip), `data/embed_shards/`(임베딩 캐시), 잠금 파일, `.env` |

각 파일의 크기·sha256이 팩 안 `manifest.json`에 있고, 팩에는 만든 사람·시각·git 커밋·임베딩 모델·제도 수가 기록된다.

## 다른 데이터와의 관계
- **검수 판정·질문(`reviews/`)과 제도 목록 관리(`catalog/events/`)는 이미 git으로 공유**되므로 팩과 별개다. 제도 숨김은 git pull만으로 반영된다.
- 제도 **추가·수정**(catalog)이 색인에 들어가려면 조장이 `python -m src.catalog apply`를 한 뒤 팩을 새로 만든다. 그러면 팀원은 apply를 따로 할 필요 없이 팩만 설치하면 된다.
- A의 수집(`pipeline`, `handoff --apply`)으로 데이터를 갱신하는 사람도 조장 한 명으로 정한다. 다른 사람이 각자 수집·색인하면 다시 데이터가 갈라진다.

## 알아 둘 점
- **팩 설치 = 내 DB·색인을 통째로 교체**한다. 내 컴퓨터에서 따로 만든 색인이 있다면 백업 폴더에 남는다.
- 임베딩 모델이 다르면 설치를 막는다(질문 벡터가 색인과 맞지 않게 된다). 모델 설정은 `src/common/config.py`의 `EMBEDDING_MODEL`.
- 색인은 chromadb 버전이 크게 다르면 못 읽을 수 있다. 팩의 `manifest.json`에 만든 쪽 chromadb 버전이 적혀 있다 (`pip install -r requirements.txt`로 맞춘다).
- Windows에서 앱·explain·handoff가 색인을 열고 있으면 "파일이 사용 중" 오류가 난다. 그때는 모두 끄고 다시 실행한다 (기존 데이터는 그대로다).
- 팩은 자주 만들 필요가 없다. 수집·색인을 새로 했거나 지역 보충·catalog 반영 같은 데이터 변경이 있을 때만 만든다.
- 이전 팩이 필요하면 공유 드라이브에서 지우지 말고 둔다 (`LATEST.json`만 최신을 가리킨다).
