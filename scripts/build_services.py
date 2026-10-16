"""[A. 데이터] 원본(raw) → 팀 공통 형식(services.jsonl)으로 정리

사용법:
    python scripts/build_services.py

목록(serviceList)과 지원조건(supportConditions)을 서비스ID로 합친다.
필드명은 2026-10-16 실제 응답으로 확인함.
"""
import json
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from src.config import PROCESSED_DIR, RAW_DIR  # noqa: E402

# API 응답 필드명 → 우리 스키마 필드명
FIELD = {
    "service_id": "서비스ID",
    "name": "서비스명",
    "summary": "서비스목적요약",
    "agency": "소관기관명",
    "category": "서비스분야",
    "support_type": "지원유형",
    "target": "지원대상",
    "criteria": "선정기준",
    "content": "지원내용",
    "how_to_apply": "신청방법",
    "deadline": "신청기한",
    "reception": "접수기관",
    "contact": "전화문의",
    "url": "상세조회URL",
}
AGENCY_TYPE_FIELD = "소관기관유형"   # 예: "중앙행정기관"

# 지원조건의 나이 하한/상한 (실제 응답: 숫자형, 예 18 / 65)
AGE_MIN_FIELD = "JA0110"
AGE_MAX_FIELD = "JA0111"

# 1단계: 개인 대상만 사용 (실제 응답 예: "가구", "개인||가구")
USER_TYPE_FIELD = "사용자구분"
PERSONAL_TYPES = ("개인", "가구")

SIDO = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
        "경기", "강원", "충청북", "충청남", "충북", "충남", "전라북", "전북",
        "전라남", "전남", "경상북", "경북", "경상남", "경남", "제주"]


def clean_text(value) -> str:
    """\\r\\n 정리, 3줄 이상 빈 줄 축소, 앞뒤 공백 제거."""
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)      # 줄 끝 공백 제거
    text = re.sub(r"\n{3,}", "\n\n", text)      # 빈 줄 여러 개 → 하나
    return text.strip()


def to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def guess_region(agency: str, agency_type: str) -> str:
    """중앙행정기관이면 '전국', 기관명이 시·도로 시작하면 그 시·도, 그 외는 '전국'."""
    if agency_type == "중앙행정기관":
        return "전국"
    for name in SIDO:
        if agency.startswith(name):
            return agency.split()[0]
    return "전국"


def main():
    services_raw = json.loads((RAW_DIR / "serviceList.json").read_text(encoding="utf-8"))
    cond_path = RAW_DIR / "supportConditions.json"
    conditions = json.loads(cond_path.read_text(encoding="utf-8")) if cond_path.exists() else []
    cond_by_id = {c.get(FIELD["service_id"]): c for c in conditions}

    # 필드명이 틀렸을 때 전부 걸러지는 사고 방지: 필드가 아예 없으면 필터를 끈다
    has_type_field = any(USER_TYPE_FIELD in raw for raw in services_raw)
    if not has_type_field:
        print(f"⚠️ '{USER_TYPE_FIELD}' 필드가 응답에 없습니다. 대상 필터 없이 전체를 저장합니다.")

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROCESSED_DIR / "services.jsonl"
    count, skipped, no_cond = 0, 0, 0
    with out.open("w", encoding="utf-8") as f:
        for raw in services_raw:
            sid = raw.get(FIELD["service_id"])
            if not sid:
                continue
            # 한 서비스에 '개인||가구'처럼 여러 값이 있을 수 있어 포함 여부로 판단
            user_type = clean_text(raw.get(USER_TYPE_FIELD))
            if has_type_field and not any(t in user_type for t in PERSONAL_TYPES):
                skipped += 1
                continue

            cond = cond_by_id.get(sid)
            if cond is None:
                no_cond += 1
                cond = {}

            service = {k: clean_text(raw.get(v)) for k, v in FIELD.items()}
            service["region"] = guess_region(service["agency"], clean_text(raw.get(AGENCY_TYPE_FIELD)))
            service["min_age"] = to_int(cond.get(AGE_MIN_FIELD), 0)
            service["max_age"] = to_int(cond.get(AGE_MAX_FIELD), 200)
            service["user_type"] = user_type
            service["source"] = "gov24"
            f.write(json.dumps(service, ensure_ascii=False) + "\n")
            count += 1

    print(f"{count}건 저장 (개인 대상 아님 {skipped}건 제외): {out}")
    if no_cond:
        print(f"ℹ️ 지원조건이 없는 서비스 {no_cond}건 → 나이 0~200(제한 없음)으로 저장. "
              "일부만 수집했다면 전체를 수집하세요.")


if __name__ == "__main__":
    main()