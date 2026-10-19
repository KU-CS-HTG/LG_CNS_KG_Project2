import json
from unittest.mock import Mock

import pytest
import requests

from src.common import config
from src.ingest import client


def envelope(rows, page=1, total=None, per_page=2):
    return dict(data=rows, page=page, perPage=per_page, currentCount=len(rows),
                totalCount=len(rows) if total is None else total)


def collect(pages, **kwargs):
    def fetch(endpoint, **params):
        response = pages[params["page"] - 1]
        if isinstance(response, Exception):
            raise response
        return response
    return client.collect_endpoint("serviceList", run_id=client.new_run_id(), per_page=2,
                                   fetch_page=fetch, **kwargs)


def test_pagination_and_preserved_envelopes(monkeypatch):
    monkeypatch.setitem(client.VERIFIED_CONTRACTS, "serviceList", True)
    result = collect([envelope([{"서비스ID": "1"}, {"서비스ID": "2"}], total=3),
                      envelope([{"서비스ID": "3"}], page=2, total=3)])
    assert result.status == "complete" and result.can_close_missing
    assert [r["서비스ID"] for r in result.rows] == ["1", "2", "3"]
    assert len(list(config.RAW_DIR.glob("*/*-0000*.json"))) == 2


@pytest.mark.parametrize("last", [client.CollectionError("http_503"), {},
    envelope([], page=2, total=3), envelope([{"서비스ID": "3"}], page=2, total=4)])
def test_intermediate_failure_preserves_success(last):
    result = collect([envelope([{"서비스ID": "1"}, {"서비스ID": "2"}], total=3), last])
    assert result.status == "partial" and not result.can_close_missing
    assert result.failed_page == 2
    assert len(result.rows) == 2
    assert list(config.RAW_DIR.glob("*/serviceList-00001.json"))


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": []},
    envelope([]), envelope([{"서비스ID": ""}]),
    {**envelope([{"서비스ID": "1"}]), "currentCount": 2}])
def test_malformed_or_empty_never_complete(payload):
    result = collect([payload])
    assert result.status == "failed" and result.diagnostics
    assert not result.can_close_missing


def test_sample_always_limited_even_when_every_page_succeeds(monkeypatch):
    monkeypatch.setitem(client.VERIFIED_CONTRACTS, "serviceList", True)
    result = collect([envelope([{"서비스ID": "1"}])], max_pages=1)
    assert result.status == "sample" and not result.can_close_missing


def test_repeated_pages_and_conflicting_ids():
    first = [{"서비스ID": "1", "value": "a"}, {"서비스ID": "2"}]
    repeated = collect([envelope(first, total=4), envelope(first, page=2, total=4)])
    assert repeated.status == "partial" and "repeated_page" in repeated.diagnostics
    conflict = collect([envelope(first, total=3), envelope([{"서비스ID": "1", "value": "b"}], page=2, total=3)])
    assert conflict.conflicts == ["1"]
    assert [r["서비스ID"] for r in conflict.rows] == ["2"]
    assert not conflict.can_close_missing


def response(status=200, payload=None, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = payload
    return result


def test_rate_limit_and_transient_retries(monkeypatch):
    get = Mock(side_effect=[response(429, headers={"Retry-After": "4"}),
                            requests.Timeout("serviceKey=SHOULD-NOT-LEAK"), response(payload={"data": []})])
    sleep = Mock()
    monkeypatch.setattr(client.requests, "get", get)
    monkeypatch.setattr(client.time, "sleep", sleep)
    assert client._get("serviceList") == {"data": []}
    assert get.call_count == 3 and sleep.call_args_list[0].args == (4,)
    assert get.call_args.kwargs["timeout"] == (10, 30)


@pytest.mark.parametrize("remote, attempts", [
    (response(403), 1), (response(503), 3),
    (response(payload={"code": 20, "message": "serviceKey=SECRET"}), 1),
    (response(payload={"code": "23"}), 3), (response(payload=[]), 1),
])
def test_safe_errors_and_retry_exhaustion(monkeypatch, remote, attempts):
    get = Mock(return_value=remote)
    monkeypatch.setattr(client.requests, "get", get)
    monkeypatch.setattr(client.time, "sleep", lambda delay: None)
    with pytest.raises(client.CollectionError) as exc:
        client._get("serviceDetail")
    assert get.call_count == attempts
    assert "SECRET" not in str(exc.value) and "serviceKey" not in str(exc.value)


def test_malformed_json_and_missing_credentials(monkeypatch):
    remote = response()
    remote.json.side_effect = ValueError("serviceKey=SECRET")
    get = Mock(return_value=remote)
    monkeypatch.setattr(client.requests, "get", get)
    with pytest.raises(client.CollectionError, match="malformed_json"):
        client._get("serviceDetail")
    monkeypatch.setattr(config, "GOV24_API_KEY", "")
    with pytest.raises(client.CollectionError, match="missing_credentials"):
        client._get("serviceList")
    assert get.call_count == 1


def test_raw_secrets_redacted():
    client.save_raw("serviceList", [{"serviceKey": config.GOV24_API_KEY,
        "echo": "https://host/?serviceKey=another-secret&x=1", "value": config.GOV24_API_KEY}])
    text = next(config.RAW_DIR.glob("*/*.json")).read_text(encoding="utf-8")
    assert config.GOV24_API_KEY not in text and "another-secret" not in text
    assert json.loads(text)[0]["serviceKey"] == "[REDACTED]"
