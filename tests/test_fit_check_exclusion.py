"""적합성 점검: duplicate로 잘못 단 후보 원문의 제외 조건을 mismatch로 받는다 (src/generate/fit_check._valid)."""
from src.generate import fit_check

OWN = ("[지원대상] 직업훈련 참여자 중 기준 중위소득 100%이하인 자\n- 전직실업자인 경우 실업급여 수급중인 자는 제외\n"
       "[지원내용] 훈련기간 중 생계비 대부, 1인당 1,000만원 이내")
IN_USE = "실업급여: 회사를 그만둔 사람에게 구직급여를 지급"
CANDS, NAMES = {"149200000037": OWN}, {"149200000037": "직업훈련 생계비대부사업"}
REASON = "사용자가 실업급여를 받고 있어 이 제도의 지원 대상에서 제외된다."


def _item(quote, category="duplicate"):
    return {"id": "149200000037", "fit": "poor", "category": category, "quote": quote, "reason": REASON}


def test_exclusion_quote_from_candidate_is_accepted_as_mismatch():
    item = _item("실업급여 수급중인 자는 제외")
    assert fit_check._valid(item, CANDS, IN_USE, NAMES)
    assert item["category"] == "mismatch"


def test_benefit_quote_from_candidate_is_still_rejected():
    """제외 말이 없는 혜택 설명 구절로는 duplicate를 받아 주지 않는다 (맞는 제도를 잘못 빼지 않게)."""
    item = _item("훈련기간 중 생계비 대부")
    assert not fit_check._valid(item, CANDS, IN_USE, NAMES)
    assert item["category"] == "duplicate"


def test_made_up_exclusion_is_rejected():
    assert not fit_check._valid(_item("재학생은 지원 대상에서 제외"), CANDS, IN_USE, NAMES)


def test_real_duplicate_from_in_use_text_unchanged():
    item = _item("회사를 그만둔 사람에게 구직급여를 지급")
    assert fit_check._valid(item, CANDS, IN_USE, NAMES)
    assert item["category"] == "duplicate"
