import pytest

from src.common import config
from src.ingest import chunker, store
from src.ingest.normalize import parse_deadline


def service(**kwargs):
    return store.clean_service({"서비스ID": "A", "서비스명": "청년 지원", "지원대상": "만 19~34세",
        "소관기관명": "서울 기관", "상세조회URL": "https://www.gov.kr/synthetic/A", **kwargs})


def verified(ranges=None, region=None, source="applicant"):
    return {"_verified_eligibility": {"source": source, "age_ranges": ranges, "region": region}}


def test_normalization_preserves_units_negation_lists():
    value = service(지원대상="청년<br>1. 중위소득 100% 이하\r\n2. 10만 원 &amp; 예외: 중복 불가")
    assert value["support_target"] == "청년\n1. 중위소득 100% 이하\n2. 10만 원 & 예외: 중복 불가"
    assert "region" not in store.build_metadata(value)  # agency is not residence
    assert "age_min" not in store.build_metadata(value)  # no speculative NLP age


def test_youth_age_overlap_broad_adult_and_dependent_age():
    raw = {"서비스ID": "A", "서비스명": "일반 지원"}
    adult = store.clean_service(raw, verified([[18, 65]]))
    assert store.is_youth_service(adult)
    assert adult["inclusion_reasons"] == [{"reason": "verified_age_overlap", "range": [18, 65]}]
    child = store.clean_service(raw, verified([[0, 39]], source="dependent"))
    assert not store.is_youth_service(child)
    assert not store.is_youth_service(store.clean_service(raw, {"JA0110": 19, "JA0111": 34}))
    broad = service(서비스명="일반 구직 상담", 지원대상="모든 성인")
    assert store.is_youth_service(broad)
    assert {"reason": "keyword", "field": "service_name", "keyword": "구직"} in broad["inclusion_reasons"]
    assert config.YOUTH_AGE_RANGE == (19, 39)


def test_disjoint_ranges_and_nationwide_unknown():
    raw = {"서비스ID": "A", "서비스명": "일반 지원"}
    value = store.clean_service(raw, verified([[10, 15], [30, 35]], "전국"))
    metadata = store.build_metadata(value)
    assert metadata["region"] == "전국" and "age_min" not in metadata
    assert store.is_youth_service(value)
    unknown = store.clean_service(raw)
    assert "region" not in store.build_metadata(unknown)
    assert "age_min" not in store.build_metadata(unknown)
    assert unknown["inclusion_policy"]["outcome"] == "provisional"


@pytest.mark.parametrize("text", ["상시", "예산 소진 시", "별도 공고", "2026년 말", "12.31", "2026-12-31 또는 예산 소진 시",
                                 "2026-02-30", "2026-12-31 ~ 2026-01-01"])
def test_ambiguous_deadlines_preserved(text):
    value = service(신청기한=text)
    assert value["application_period"] == text and value["deadline"] is None
    assert "deadline" not in store.build_metadata(value)


@pytest.mark.parametrize("text, expected", [("2026.12.31", "2026-12-31"), ("2026-01-01 ~ 2026-12-31", "2026-12-31")])
def test_unambiguous_deadlines(text, expected):
    assert parse_deadline(text) == expected


def test_provenance_missing_not_invented():
    value = service(소관기관명=None, 상세조회URL="https://evil.example/pretend")
    assert value["detail_url"] == "" and "agency" not in store.build_metadata(value)
    assert value["data_quality"] == ["missing_agency", "missing_detail_url"]


def test_condition_join_mismatch_rejected():
    with pytest.raises(ValueError, match="condition_id_mismatch"):
        store.clean_service({"서비스ID": "A", "서비스명": "청년"}, {"서비스ID": "B"})


@pytest.mark.parametrize("text", ["문장. 다음 문장! 끝? " * 40, "줄1\n\n줄2\n" * 50, "가나다" * 150, "", " a \n b "])
def test_recursive_content_preservation(text):
    parts = chunker.split_long_text(text, 23)
    assert "".join(parts) == text
    assert all(0 < len(p) <= 23 for p in parts)
    assert parts == chunker.split_long_text(text, 23)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_limits(limit):
    with pytest.raises(ValueError):
        chunker.split_long_text("text", limit)
    with pytest.raises(ValueError):
        chunker.chunk_service(service(), limit)


def test_chunk_ids_prefix_metadata_empty_and_shortening():
    value = service(지원대상="청년 대상\n예외: 제외 아님. " * 20, 선정기준="", 지원내용=" ", 신청방법="방문")
    chunks = chunker.chunk_service(value, max_chars=50)
    assert chunks == chunker.chunk_service(value, max_chars=50)
    assert len({c.chunk_id for c in chunks}) == len(chunks)
    assert all(c.text.startswith("청년 지원\n") and len(c.text) <= 50 for c in chunks)
    assert all(c.detail_url == value["detail_url"] for c in chunks)
    target = [c for c in chunks if c.section == "지원대상"]
    assert target[0].chunk_id == "A#지원대상-1"
    assert "".join(c.text[len("청년 지원\n"):] for c in target) == value["support_target"]
    short = chunker.chunk_service(service(지원대상="짧은 청년", 신청방법=""))
    assert [c.chunk_id for c in short] == ["A#지원대상"]
    assert chunker.chunk_service(service(지원대상="")) == []
    with pytest.raises(ValueError):
        chunker.chunk_service(value, len(value["service_name"]) + 1)
