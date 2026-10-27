"""X1 판정 기준 문서 PDF -> GuideChunk (src/ingest/guides.py). 실제 PDF 없이 쪽 텍스트로 시험한다."""
import hashlib
import json

import pytest

from src.common.schema import GuideChunk
from src.ingest import guides

SOURCE = {"doc_id": "doc-a", "file": "a.pdf", "title": "2026년 시험 사업안내", "publisher": "보건복지부",
          "year": 2026, "source_url": "https://example.go.kr/a", "scope": "전국", "service_ids": ["S1"]}


def _pages(n=6):
    """머리말(쪽 번호만 다름)과 쪽 번호 줄이 있는 n쪽짜리 문서. 본문은 쪽마다 다르다."""
    topics = ["소득인정액", "부양의무자", "재산 환산", "근로소득공제", "보장가구", "자동차 재산", "금융재산", "주거급여"]
    return [f"| 2026년 시험 사업안내 |\n❙ {100 + i}\n본문: {topics[i]} 기준을 이 쪽에서 설명한다. 수급자 선정에 쓴다.\n- {i + 1} -"
            for i in range(n)]


def test_committed_sources_are_complete():
    docs = guides.load_sources()
    assert docs and len({d["doc_id"] for d in docs}) == len(docs)
    for d in docs:
        assert d["file"].endswith(".pdf") and len(d["sha256"]) == 64 and d["publisher"] and d["year"]
        assert d["scope"] == "전국" and isinstance(d["service_ids"], list)


def test_repeated_headers_and_page_numbers_are_removed():
    chunks = guides.doc_chunks(SOURCE, _pages())
    assert [c.page for c in chunks] == [1, 2, 3, 4, 5, 6]          # 쪽 번호 = PDF 쪽 순서 (1부터)
    for c in chunks:
        assert "사업안내 |" not in c.text and "❙" not in c.text and not c.text.rstrip().endswith("-")
        assert c.text.startswith("본문")


def test_chunk_fields_follow_source():
    c = guides.doc_chunks(SOURCE, _pages())[2]
    assert isinstance(c, GuideChunk)
    assert c.chunk_id == "doc-a#p3-1" and c.doc_title == SOURCE["title"] and c.publisher == "보건복지부"
    assert c.year == 2026 and c.scope == "전국" and c.service_ids == ["S1"] and c.source_url == SOURCE["source_url"]


def test_headings_carry_across_pages():
    pages = ["제2장 소득인정액 산정\n1. 소득평가액\n근로소득은 일을 해서 번 소득이다. " * 2,
             "근로소득공제는 30%를 적용한다. 대학생 아르바이트 소득은 별도 공제한다.",
             "2. 재산의 소득환산액\n재산은 일반재산·금융재산·자동차로 나눈다. 자동차는 재산으로 본다."]
    chunks = guides.doc_chunks(SOURCE, pages)
    assert chunks[0].heading == "2026년 시험 사업안내 > 제2장 소득인정액 산정 > 1. 소득평가액"
    assert chunks[1].heading == chunks[0].heading                      # 다음 쪽도 같은 절
    assert chunks[2].heading.endswith("> 2. 재산의 소득환산액")


def test_letter_spaced_words_are_joined():
    lines = guides.clean_lines("제2편. 신 청 및 선 정 기 준\n및 그 외 비용\n재 참 여 제 한", set())
    assert lines == ["제2편. 신청및선정기준", "및 그 외 비용", "재참여제한"]       # 3글자 이하는 그대로


def test_roman_numeral_heading_and_toc_lines():
    assert guides.heading_level("2 Ⅰ유형 요건(구직촉진수당 수급요건)") == 2
    assert guides.heading_level("Ⅲ-4. 재참여제한 119p 참고") == 0
    raw = guides.clean_lines("Ⅲ - 4 . 재 참 여 제 한 1 1 9 p 참 고", set())[0]       # 실제 PDF에서 뽑힌 모양
    assert guides.heading_level(raw) == 0
    assert guides.heading_level("제2장 소득인정액 ···· 35") == 0
    assert guides.heading_level("1. 의미") == 1


def test_long_page_is_split_with_overlap_and_stays_on_page():
    lines = [f"{i}번째 줄입니다. 수급자의 소득과 재산을 조사한다." for i in range(80)]
    chunks = guides.doc_chunks(SOURCE, ["\n".join(lines)])
    assert len(chunks) > 1 and all(c.page == 1 for c in chunks)
    assert all(len(c.text) <= guides.MAX_CHARS for c in chunks)
    first_end = chunks[0].text.splitlines()[-1]
    assert first_end in chunks[1].text                                 # 경계 문장을 양쪽에서 찾을 수 있다
    assert [c.chunk_id for c in chunks][:2] == ["doc-a#p1-1", "doc-a#p1-2"]


def test_empty_pages_are_dropped():
    chunks = guides.doc_chunks(SOURCE, ["", "  \n12\n", "본문이 충분히 긴 쪽입니다. 생계급여 선정기준을 설명한다."])
    assert [c.page for c in chunks] == [3]


def test_verify_stops_on_missing_or_different_file(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.7 original")
    good = {**SOURCE, "sha256": hashlib.sha256(b"%PDF-1.7 original").hexdigest()}
    guides.verify([good], tmp_path)
    with pytest.raises(guides.GuideSourceError, match="지문 다름"):
        guides.verify([{**good, "sha256": "0" * 64}], tmp_path)
    with pytest.raises(guides.GuideSourceError, match="없음"):
        guides.verify([{**good, "file": "b.pdf"}], tmp_path)


def test_build_chunks_verifies_before_parsing(tmp_path, monkeypatch):
    (tmp_path / "a.pdf").write_bytes(b"changed")
    monkeypatch.setattr(guides, "page_texts", lambda p: pytest.fail("지문이 다르면 파싱하지 않는다"))
    with pytest.raises(guides.GuideSourceError):
        guides.build_chunks([{**SOURCE, "sha256": "0" * 64}], tmp_path)


def test_write_and_read_round_trip(tmp_path):
    chunks = guides.doc_chunks(SOURCE, _pages())
    path = tmp_path / "chunks.jsonl"
    guides.write_chunks(chunks, path)
    assert guides.read_chunks(path) == chunks
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["doc_id"] == "doc-a"
