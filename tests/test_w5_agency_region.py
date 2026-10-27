"""W5: 법인 표시·시·군·구 이름으로 시작하는 지자체 산하 기관의 지역 (generator._agency_region / _agency_districts).

'재단법인울산문화관광재단' 시티투어 감면이 전국 제도로 보여 '수원 대중교통비' 질문에 3번 카드로 나왔다."""
import pytest

from src.generate import generator as g


@pytest.mark.parametrize("agency, region", [
    ("재단법인울산문화관광재단", "울산"),
    ("재단법인경기도시장상권진흥원", "경기"),
    ("(재)충북문화재단", "충북"),
    ("재단법인세종특별자치시인재육성평생교육진흥원", "세종"),
    ("(재)인천인재평생교육진흥원", "인천"),
    ("(주)강원랜드", "강원"),
    ("고양산업진흥원", "경기"),
    ("거제해양관광개발공사", "경남"),
    ("(재)부천산업진흥원", "경기"),
    ("단양관광공사", "충북"),
    ("재단법인영등포문화재단", "서울"),
    ("(재)남동문화재단", "인천"),
    ("재단법인여주세종문화재단", "경기"),            # 이름 중간의 '세종'은 세종시가 아니다
    ("재단법인광주시문화재단", "경기"),               # 경기 광주시 (광주광역시 아님)
])
def test_local_agencies_get_their_region(agency, region):
    assert g._agency_region(agency) == region


@pytest.mark.parametrize("agency", [
    "보건복지부", "한국장학재단", "신용보증재단중앙회", "한국남동발전(주)", "중소벤처기업진흥공단",
    "재단법인세종문화회관", "재단법인세종연구원", "대한법률구조공단", "서민금융진흥원",
])
def test_national_agencies_stay_national(agency):
    assert g._agency_region(agency) is None


def test_existing_regions_unchanged():
    assert g._agency_region("충청북도 충주시") == "충북"
    assert g._agency_region("재단법인의왕시청소년재단") == "경기"
    assert g._agency_region("광주신용보증재단") == "광주"


@pytest.mark.parametrize("agency, districts", [
    ("재단법인영등포문화재단", {"영등포구"}),
    ("고양산업진흥원", {"고양시"}),
    ("(재)부천시여성청소년재단", {"부천시"}),
    ("광주신용보증재단", set()),                 # 광주광역시 기관 (경기 광주시 아님)
    ("제주관광공사", set()),                     # 도 기관 (제주시 아님)
    ("충청남도천안의료원", set()),               # 도 의료원은 도 전체
])
def test_districts(agency, districts):
    assert g._agency_districts(agency) == districts
