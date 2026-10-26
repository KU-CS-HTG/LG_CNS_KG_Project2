"""카드 상세 기능(W1~W4)의 기반: 새 선택 필드의 기본값과 자리표시자가 기존 동작을 바꾸지 않는지 확인한다."""
from src.common.schema import Chunk, Recommendation
from src.generate import apply_link, benefit_detail, exclusion, period_detail


def _rec() -> Recommendation:
    return Recommendation(service_name="s", agency="a", reason="r", benefit="b", how_to_apply="h")


def test_new_recommendation_fields_default_to_empty():
    r = _rec()
    assert r.benefit_detail == [] and r.apply_url == "" and r.period_detail == ""
    assert _rec().benefit_detail is not r.benefit_detail            # 인스턴스마다 따로 (공유 리스트 아님)


def test_placeholders_change_nothing():
    chunks = [Chunk(chunk_id="S1#지원내용", service_id="S1", service_name="n", section="지원내용", text="t")]
    assert exclusion.exclude_in_use("질문", {}, chunks) is chunks
    assert benefit_detail.extract(chunks) == []
    assert apply_link.find(chunks, "온라인 신청") == ""
    assert period_detail.explain("상시신청") == ""


def test_card_placeholders_draw_nothing():
    from app import card_apply, card_benefit, card_period
    r = _rec()
    assert card_benefit.render(r, "k") is None and card_apply.render(r, "k") is None and card_period.render(r, "k") is None
