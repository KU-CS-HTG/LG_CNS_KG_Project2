"""특정 대상 전용 제도 거르기 확대 (generator._other_group, 2026-10-27): 농어촌·다자녀·재창업·기업 대상."""
import pytest

from src.generate import generator as g


@pytest.mark.parametrize("question, service_name", [
    ("대학 등록금이 부담돼요. 받을 수 있는 장학금이나 학자금 지원이 있나요?", "농어촌 출신 대학(원)생 학자금 융자"),
    ("대학 등록금이 부담돼요. 받을 수 있는 장학금이나 학자금 지원이 있나요?", "청년창업농장학금 지원"),
    ("대학 등록금이 부담돼요. 받을 수 있는 장학금이나 학자금 지원이 있나요?", "다자녀(세 자녀 이상) 국가장학금"),
    ("빚이 많아서 힘든 청년이 받을 수 있는 채무 조정이나 지원이 있나요?", "재창업 재기지원 보증"),
    ("전남 목포에 사는 25살 취업 준비생인데 받을 수 있는 지원이 있나요?", "청년 일자리 매칭 및 인건비 지원"),
    ("50대인데 재취업하고 싶어요", "중장년 채용장려금 지원"),
])
def test_target_only_service_is_dropped_when_question_has_no_such_person(question, service_name):
    assert g._other_group(question, service_name)


@pytest.mark.parametrize("question, service_name", [
    ("귀농하려는 청년인데 지원이 있나요?", "청년창업농 영농기반 임차지원"),
    ("시골 출신 대학생인데 학자금 지원 있나요?", "농어촌 출신 대학(원)생 학자금 융자"),
    ("아이 둘 키우는데 받을 수 있는 혜택", "다자녀가족 둘째아 이상 초등학교 입학축하상품권 지원"),
    ("형제가 셋인 대학생인데 장학금 있나요?", "다자녀(세 자녀 이상) 국가장학금"),
    ("작은 가게 사장인데 직원 인건비 지원 있나요?", "청년 일자리 매칭 및 인건비 지원"),
    ("폐업하고 다시 창업하려고 해요", "재창업자금"),
])
def test_kept_when_question_mentions_that_person(question, service_name):
    assert not g._other_group(question, service_name)


BUPYEONG = "인천 부평구에 사는 27살인데 전세 보증금 지원 받을 수 있어?"


@pytest.mark.parametrize("service_name", [
    "부도 임대주택 퇴거자 전세자금 융자", "인천 전세사기 피해자 주거안정 지원사업(긴급생계비)",
    "동작구 전세피해임차인 지원금(이사비 실비 지원)", "화재피해 주민 지원"])
def test_victim_only_services_dropped_without_victim_talk(service_name):
    assert g._other_group(BUPYEONG, service_name)


@pytest.mark.parametrize("question", [
    "전세사기를 당했는데 받을 수 있는 지원이 있나요?", "집주인이 보증금을 못 돌려줘요", "살던 임대주택이 부도나서 나가야 해요",
    "전세 피해를 입었어요"])
def test_victim_services_kept_when_question_says_so(question):
    assert not g._other_group(question, "인천 전세사기 피해자 주거안정 지원사업(긴급생계비)")
    assert not g._other_group(question, "부도 임대주택 퇴거자 전세자금 융자")


def test_guarantee_fee_goes_behind_victim_programs_for_victims():
    q = "전세사기를 당했는데 보증금을 못 돌려받고 있어요. 받을 수 있는 지원이 있나요?"
    assert g._prevention_for_victim(q, [], "국토교통부 전세보증금반환보증 보증료 지원")
    assert g._prevention_for_victim("도와주세요", ["전세사기피해(전세사기·보증금 미반환 피해를 실제로 당함)"],
                                    "전세보증금반환보증 보증료 지원")
    assert not g._prevention_for_victim(q, [], "구로구 전세사기피해지원금(보증료) 지원")       # 피해자 전용은 그대로
    assert not g._prevention_for_victim(BUPYEONG, [], "국토교통부 전세보증금반환보증 보증료 지원")  # 피해 전 질문은 그대로


def test_damage_prevention_is_not_victim_only():
    assert not g._other_group("귀농하려는 청년인데 지원이 있나요?", "야생동물 피해예방시설 설치 지원")
    assert not g._other_group("축산업 하는데 지원 있나요?", "축산 폭염피해 방지 냉방기 지원")
    assert not g._other_group(BUPYEONG, "버팀목전세자금대출")


def test_small_business_only_for_business_owners():
    quit_job = "회사를 그만두고 실업급여 받는 중인데 직업훈련 지원도 받을 수 있나요?"
    assert g._other_group(quit_job, "소상공인 재기지원")
    assert not g._other_group("혼자 작은 가게를 열려고 하는 청년이 받을 수 있는 정책 자금이 있나요?", "혁신 소상공인 창업지원")
    assert not g._other_group("식당을 하는데 장사가 안 돼서 힘들어요", "소상공인 경영안정자금지원")
    assert not g._other_group(quit_job, "국민내일배움카드")


def test_bereaved_and_medical_aid_only():
    q = "군대 전역한 23살인데 받을 수 있는 지원이 있나요?"
    assert g._other_group(q, "6ㆍ25전사자(미수습) 유가족 유전자 채취 포상금 및 전사자 유해 소재 제보 포상금 지급 안내")
    assert g._other_group(q, "의료급여 (의료급여 건강생활 유지비)")
    assert not g._other_group("기초생활수급자인데 틀니 지원 받을 수 있나요?", "의료급여 틀니·치과임플란트 지원")
    assert not g._other_group("아버지가 순직하셨는데 유족이 받을 수 있는 지원", "군인 재해보상 유족급여 지급")
    assert not g._other_group(q, "전역예정군인 재취업 지원")


def test_general_services_not_affected():
    q = "대학 등록금이 부담돼요. 받을 수 있는 장학금이나 학자금 지원이 있나요?"
    for name in ["국가장학금 Ⅰ유형 (학생직접지원형)", "취업 후 상환 학자금대출", "지역인재장학금", "국가근로장학금"]:
        assert not g._other_group(q, name)


def test_existing_groups_unchanged():
    assert g._other_group("졸업했는데 취업을 못 했어요", "장애인 취업성공패키지")
    assert not g._other_group("장애가 있는 구직자인데 취업 지원 있나요?", "장애인 취업성공패키지")
