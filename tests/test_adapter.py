import httpx
import pytest

from benefit_rag.gov24 import Gov24Adapter, SourceError, normalize


@pytest.mark.parametrize("status", [429, 503])
def test_bounded_retries_and_timeout(status):
    requests, delays = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, content=b"retry", headers={"Retry-After": "2"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = Gov24Adapter(client, "test-only", sleep=delays.append).fetch_page(1, 2)
    assert result.status == status and len(requests) == 3
    assert delays == [2, 2]
    assert requests[0].extensions["timeout"]["connect"] == 5
    assert requests[0].extensions["timeout"]["read"] == 20
    assert requests[0].url.params["serviceKey"] == "test-only"


def test_retry_after_exceeding_budget_does_not_retry_early():
    requests, delays = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = Gov24Adapter(client, "test-only", sleep=delays.append).fetch_page(1, 2)
    assert result.status == 429 and len(requests) == 1 and not delays


@pytest.mark.parametrize("status", [401, 403, 302])
def test_auth_and_redirect_are_not_retried(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://example.com"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert Gov24Adapter(client, "test-only").fetch_page(1, 2).status == status
    assert len(requests) == 1


def test_transport_error_is_sanitized_and_bounded():
    delays = []

    def handler(request):
        raise httpx.ReadTimeout("secret-in-original-exception", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceError, match="^transport_retries_exhausted$"):
            Gov24Adapter(client, "test-only", sleep=delays.append).fetch_page(1, 2)
    assert delays == [1, 2]


def test_bad_optional_types_url_and_geography_stay_unknown():
    data = normalize(
        {
            "서비스ID": "SYNTHETIC",
            "서비스명": "[합성]",
            "지원대상": ["not a string"],
            "소관기관명": "[합성] 서울 기관",
            "상세조회URL": "javascript:alert(1)",
        },
        0,
    )
    assert data.target_text is data.official_url is data.geographic_coverage is None
    assert set(data.quality_issues) == {"invalid_type:target_text", "invalid_url"}
    assert data.eligibility_status == "unresolved"
