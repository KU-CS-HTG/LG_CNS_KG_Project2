# LG_CNS_KG_Project2
RAG 기반 정부 지원제도 맞춤 안내 서비스

사용자의 나이·거주지를 받아 **조건은 메타데이터 필터로 확정**하고, **지원내용·신청방법은 벡터 검색 + LLM**으로 안내합니다.

## 폴더 구조
```
├── app.py                     # [C] Streamlit 데모
├── src/
│   ├── config.py              # 경로·키·모델 설정
│   ├── schema.py              # ★ 팀 공통 데이터 형식 (먼저 합의)
│   ├── retriever.py           # [B] 조건 필터 + 벡터 검색
│   └── generator.py           # [C] 근거 기반 답변 생성
├── scripts/
│   ├── collect_gov24.py       # [A] 보조금24 API 원본 수집 → data/raw/
│   ├── build_services.py      # [A] 원본 → data/processed/services.jsonl
│   └── build_index.py         # [B] services.jsonl → chroma_db/
├── eval/
│   ├── questions.jsonl        # 평가 질문 (사용자 조건 + 기대/제외 제도)
│   └── run_eval.py            # 검색 정확도 확인
└── data/sample/services_sample.jsonl   # API 없이 테스트용 가상 데이터
```

## 시작하기
```bash
pip install -r requirements.txt
copy .env.example .env          # Windows (mac/linux: cp) → 키 입력
```

## 실행 순서
```bash
# 0) API 없이 샘플로 전체 흐름 먼저 확인
python scripts/build_index.py --input data/sample/services_sample.jsonl
python eval/run_eval.py
streamlit run app.py

# 1) 실제 데이터
python scripts/collect_gov24.py --pages 3 --per-page 100
python scripts/build_services.py
python scripts/build_index.py
```

## 협업 규칙
- main에는 직접 push하지 않고 각자 브랜치에서 작업합니다.
- `.env`(API 키)는 절대 커밋하지 않습니다. `git status`에 보이면 멈추고 확인하세요.
- `src/schema.py`의 필드를 바꿀 땐 팀원 모두에게 알립니다.
