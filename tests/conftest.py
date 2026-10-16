from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from benefit_rag.db import make_engine
from benefit_rag.gov24 import FixtureAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "gov24"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = make_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def adapter():
    return FixtureAdapter(FIXTURES)
