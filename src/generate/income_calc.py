"""소득 기준 계산기 (C 담당): 가구원 수·월 소득 -> 기준 중위소득 대비 %, 카드 '확인 필요'의 '중위소득 N% 이하'와 비교.

참고용이다. 제도마다 소득을 보는 방법이 다르다 (소득인정액 = 소득평가액 + 재산의 소득환산액, 건강보험료 기준,
가구 범위 등). 그래서 자격을 단정하지 않고 '기준 안쪽/넘음(참고)'만 보여 주며 체크리스트를 대신 체크하지 않는다.
'도시근로자 월평균소득'·'연소득 N원'처럼 다른 기준은 계산하지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

YEAR = 2026
# 2026년 기준 중위소득 (원/월, 가구원 수별). 보건복지부 고시값: 2026년 국민기초생활보장사업 안내 p.14(xii)·
# 주거급여 사업안내 p.9·국민취업지원제도 업무매뉴얼 p.20·보건복지부 보도자료(2025.7.31.) p.4에서 같은 값을 확인
MEDIAN_INCOME = {1: 2_564_238, 2: 4_199_292, 3: 5_359_036, 4: 6_494_738, 5: 7_556_719, 6: 8_555_952, 7: 9_515_150}
SOURCE = f"보건복지부 {YEAR}년 기준 중위소득"
NOTICE = ("참고용이에요. 제도마다 소득을 보는 방법(재산 환산을 더한 소득인정액, 건강보험료 기준 등)과 "
          "가구 범위가 달라 실제 판정과 다를 수 있어요. 최종 자격은 담당 기관에서 확인하세요.")
COMMON_PERCENTS = (32, 40, 48, 50, 60, 100, 120, 150)   # 생계·의료·주거·교육급여, 국민취업지원 등에서 자주 쓰는 기준
MAX_HOUSEHOLD = 10

# '기준 중위소득 60% 이하', '중위소득 100분의 120 이하', '중위소득의 150%이하' (뒤에 '미만'도)
_PERCENT_RE = re.compile(r"중위\s*소득\s*(?:의\s*)?(?:(\d{1,3}(?:\.\d+)?)\s*%|100\s*분의\s*(\d{1,3}))\s*(이하|미만)?")


def median(household: int) -> int:
    """가구원 수별 기준 중위소득(원/월). 8인 이상은 1인 늘 때마다 7인 기준에 (7인 - 6인) 차이를 더한다
    (주거급여 사업안내 p.9 '8인 이상 가구' 산정 방법)."""
    if household < 1:
        raise ValueError("가구원 수는 1명 이상이어야 합니다.")
    if household <= 7:
        return MEDIAN_INCOME[household]
    return MEDIAN_INCOME[7] + (household - 7) * (MEDIAN_INCOME[7] - MEDIAN_INCOME[6])


def percent(household: int, monthly_income: int) -> float:
    """월 소득이 기준 중위소득의 몇 %인가 (소수 첫째 자리)."""
    return round(monthly_income / median(household) * 100, 1)


def amount(household: int, pct: float) -> int:
    """기준 중위소득의 pct% 금액(원/월, 원 단위 버림)."""
    return int(median(household) * pct / 100)


@dataclass
class Check:
    """'확인 필요' 항목 하나와 입력한 소득의 비교 (참고용)."""
    limit: float          # 항목의 기준 (중위소득 %)
    strict: bool          # '미만'이면 True (같으면 안쪽이 아님)
    mine: float           # 입력한 소득의 중위소득 대비 %
    limit_amount: int     # 이 가구원 수에서 기준 금액 (원/월)

    @property
    def within(self) -> bool:
        return self.mine < self.limit if self.strict else self.mine <= self.limit


def threshold(item: str) -> tuple[float, bool] | None:
    """항목에서 '중위소득 N% 이하/미만'을 읽는다 -> (N, 미만인가). 없거나 다른 기준(도시근로자 소득 등)이면 None.
    한 항목에 비율이 여러 개면(예: '60% 이하(청년은 120% 이하)') 대상별로 달라 비교하지 않는다."""
    if not item or "도시근로자" in item:
        return None
    found = _PERCENT_RE.findall(item)
    if len(found) != 1:
        return None
    pct, per100, word = found[0]
    return float(pct or per100), word == "미만"


def check(item: str, household: int | None, monthly_income: int | None) -> Check | None:
    """항목 기준과 입력한 소득을 비교. 기준을 못 읽거나 입력이 없으면 None."""
    found = threshold(item)
    if found is None or not household or monthly_income is None or monthly_income < 0:
        return None
    limit, strict = found
    return Check(limit=limit, strict=strict, mine=percent(household, monthly_income), limit_amount=amount(household, limit))


def won(n: int) -> str:
    """원 -> '256만 4,238원' (보도자료 표기)."""
    man, rest = divmod(int(n), 10_000)
    if not man:
        return f"{rest:,}원"
    return f"{man:,}만 {rest:,}원" if rest else f"{man:,}만원"
