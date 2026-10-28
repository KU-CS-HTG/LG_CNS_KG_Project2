"""신청기한 원문으로 마감이 지난 제도를 검색에서 빼기 (src/retrieve/period_rules.py, WORK_ITEMS R1 2단계). DB 없이."""
import pytest

from src.common.schema import Chunk
from src.generate import generator
from src.retrieve import period_rules

PERIODS = {"OLD": "2026년 상반기", "AGAIN": "(2026년 2학기) (1차) 2026년 5월 22일~6월 22일", "OPEN": "상시신청",
           "NEW": "2026. 11. 1. ~ 2026. 12. 31.", "NONE": ""}


def chunk(sid):
    return Chunk(chunk_id=f"{sid}#지원내용", service_id=sid, service_name=sid, section="지원내용", text=sid)


@pytest.fixture(autouse=True)
def periods(monkeypatch):
    period_rules._cache.clear()
    monkeypatch.setattr(generator, "_application_period", lambda sid: PERIODS.get(sid, ""))
    monkeypatch.setattr(generator, "_recurring_closed", lambda text, today: "학기" in text)
    yield
    period_rules._cache.clear()


RANKED = [(chunk("OLD"), 0.05), (chunk("AGAIN"), 0.04), (chunk("OPEN"), 0.03), (chunk("NEW"), 0.02), (chunk("NONE"), 0.01)]


def test_expired_dropped_recurring_moved_back_others_kept_in_order():
    out = period_rules.apply(RANKED, "2026-10-26")
    assert [c.service_id for c, _ in out] == ["OPEN", "NEW", "NONE", "AGAIN"]


def test_before_the_deadline_nothing_changes():
    out = period_rules.apply(RANKED, "2026-03-01")
    assert [c.service_id for c, _ in out][:2] == ["OLD", "OPEN"]       # 상반기 기한 전: 빼지 않는다


def test_zero_penalty_keeps_recurring_in_place():
    out = period_rules.apply(RANKED, "2026-10-26", penalty=0)
    assert [c.service_id for c, _ in out] == ["AGAIN", "OPEN", "NEW", "NONE"]
