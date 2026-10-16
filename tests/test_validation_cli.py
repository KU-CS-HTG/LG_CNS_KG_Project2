import json
import sys

import pytest

from benefit_rag import cli
from benefit_rag.ingestion import ingest


def test_profile_cli_never_constructs_http_client(engine, adapter, monkeypatch, capsys):
    run = ingest(engine, adapter, dataset_kind="fixture")

    def forbidden_client(*args, **kwargs):
        raise AssertionError("profile must not construct an HTTP client")

    monkeypatch.setattr(cli.httpx, "Client", forbidden_client)
    monkeypatch.setattr(
        sys,
        "argv",
        ["benefit-rag", "profile", "--run-id", run["run_id"], "--dataset-kind", "fixture"],
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["observed"]["object_records"] == 3


def test_evaluation_cli_does_not_open_database(monkeypatch, capsys):
    def forbidden_engine(*args, **kwargs):
        raise AssertionError("evaluation validation must not open a database")

    monkeypatch.setattr(cli, "make_engine", forbidden_engine)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benefit-rag",
            "validate-evaluation",
            "--file",
            "tests/fixtures/evaluation/synthetic.json",
        ],
    )
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["data_kind"] == "synthetic_contract"
    assert result["evidence_storage_checked"] and not result["policy_truth_verified"]


def test_invalid_evaluation_cli_does_not_print_input(tmp_path, monkeypatch, capsys):
    path = tmp_path / "invalid.json"
    path.write_text('{"private_unknown_field":"private-profile-value"}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["benefit-rag", "validate-evaluation", "--file", str(path)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "private-profile-value" not in capsys.readouterr().err
