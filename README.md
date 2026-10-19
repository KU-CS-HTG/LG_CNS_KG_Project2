# LG_CNS_KG_Project2
개인 맞춤형 정부 지원제도 탐색 및 안내 서비스 (RAG)

생활 상황을 평소 말투로 질문하면 **조건(나이·거주지)은 SQL/메타데이터 필터로 확정**하고,
**지원대상·선정기준 등 근거는 검색(SQL·BM25·Vector·Hybrid)**으로 찾아 출처와 함께 안내합니다.

## 흐름 (기획서 6.3)
```
질문 → LLM 조건 추출(+사이드바 보정) → 조건 필터 → 검색(모드 선택) → 서비스 단위 묶기·중복 제거 → 근거 인용 답변
```

## 폴더 구조
```
├── app.py                     # [C] Streamlit 데모 (검색 모드 선택 가능)
├── src/
│   ├── config.py              # 경로·키·모델·검색 파라미터
│   ├── schema.py              # 팀 공통 데이터 형식, 청크 대상 항목
│   ├── regions.py             # 시·도 이름 정규화 (데이터·사용자 입력 공통)
│   ├── chunking.py            # 구조 기반 분할 (항목별 + 긴 항목 Recursive)
│   ├── db.py                  # SQLite: 서비스·청크 저장, 조건 필터, SQL 키워드 검색
│   ├── embeddings.py          # openai / bge-m3 전환
│   ├── conditions.py          # LLM 조건 추출 + 사이드바 보정
│   ├── retriever.py           # sql / bm25 / vector / hybrid(RRF)
│   └── generator.py           # 근거 한정 답변 + 인용
├── scripts/
│   ├── collect_gov24.py       # [A] 보조금24 API 수집 → data/raw/
│   ├── build_services.py      # [A] raw → services.jsonl + services.db
│   └── build_index.py         # [B] 청크 → chroma_db_<임베딩>/
├── eval/
│   ├── questions.jsonl        # 평가 질문 (조건 + 정답/제외 서비스)
│   └── run_eval.py            # 모드별 Hit@K·Recall@K·MRR·조건위반율 → results.csv
└── data/sample/               # API 없이 테스트할 가상 데이터
```

## 시작하기 (Windows PowerShell)
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # 키 입력
```
Mac: `python3 -m venv venv` → `source venv/bin/activate` → `cp .env.example .env`

## 실행 순서
```powershell
# 0) 샘플로 전체 흐름 확인 (OpenAI 키만 있으면 됨)
python scripts/build_services.py --sample data/sample/services_sample.jsonl
python scripts/build_index.py
python eval/run_eval.py --verbose
streamlit run app.py

# 1) 실제 데이터
python scripts/collect_gov24.py --pages 110 --per-page 100
python scripts/build_services.py
python scripts/build_index.py
```

## 임베딩 비교 (bge-m3)
```powershell
pip install -r requirements-bge.txt     # PyTorch 포함, 용량 큼
# .env 에서 EMBEDDING_PROVIDER=bge 로 변경 후
python scripts/build_index.py           # chroma_db_bge/ 에 따로 저장됨
python eval/run_eval.py
```

## 협업 규칙
- main에는 직접 push하지 않고 각자 브랜치에서 작업합니다.
- `.env`(API 키)는 절대 커밋하지 않습니다.
- `src/schema.py`의 필드를 바꿀 땐 팀원 모두에게 알립니다.
