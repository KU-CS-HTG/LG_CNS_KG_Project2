"""W4: 신청기한이 설명형('과정별 모집기간 상이' 등)일 때 보여 줄 원문 전체 (docs/WORK_ITEMS.md W4).

`generator._to_recommendation`이 `Recommendation.period_detail`을 채울 때 호출한다. 설명형이 아니면 빈 문자열이다.
활성 제도 10,695개 중 1,743개가 '상이·별도·공고·문의' 같은 설명형이고 그중 1,706개는 날짜가 없다
('접수기관 별 상이' 862개). 날짜가 있으면 카드의 신청기한만으로 일정을 알 수 있어 펼치지 않는다.
과정별 일정은 API에 없으므로 앱이 만들지 않는다: 원문 그대로와 '담당 기관 확인'만 보여 준다 (app/card_period.py).
"""
from __future__ import annotations

import re

# 일정을 기관·공고에서 따로 확인해야 하는 표현
_DESCRIPTIVE_RE = re.compile(r"상이|별도|공고|공지|문의|확인|기관\s*별|과정\s*별|캠퍼스\s*별|지역\s*별|사업\s*별|"
                             r"시\s*군\s*별|홈페이지|추후|미정|변동")
# '별도의 신청절차가 없음'은 신청할 필요가 없다는 뜻이라 확인할 일정이 없다
_NO_APPLICATION_RE = re.compile(r"신청\s*절차\s*(?:가|는)?\s*없|신청\s*(?:이|은)?\s*(?:불필요|필요\s*없)")
# 연도·월일이 적혀 있으면 일정이 이미 보인다 ('2026.1.19.~1.30.', '3월 5일', '9.1.~9.15.')
_DATE_RE = re.compile(r"20\d\d|\d{1,2}\s*월\s*\d{1,2}\s*일|\d{1,2}\s*\.\s*\d{1,2}\s*\.")
# generator가 신청기한 앞에 붙이는 표시 ('신청 예정 · …', '이번 신청 마감 · 다음 공고 확인 · …').
# 원문만 판단·표시하려고 뗀다 (generator를 import하면 순환 import라 문구를 여기 둔다)
_MARK_RE = re.compile(r"^(?:신청 예정|이번 신청 마감 · 다음 공고 확인)\s*·\s*")


def explain(period: str) -> str:
    """설명형 신청기한이면 원문 전체 (줄바꿈 유지, 앞뒤 공백만 정리), 아니면 ''."""
    text = _MARK_RE.sub("", (period or "").strip()).strip()
    flat = " ".join(text.split())
    if not flat or not _DESCRIPTIVE_RE.search(flat) or _NO_APPLICATION_RE.search(flat) or _DATE_RE.search(flat):
        return ""
    return text
