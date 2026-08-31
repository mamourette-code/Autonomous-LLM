from __future__ import annotations

import pytest

from autonomous.config import Settings
from autonomous.storage import Database


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        provider="mock",
        data_dir=tmp_path,
        max_steps=5,
        watchers_enabled=False,
        _env_file=None,
    )


@pytest.fixture
def db(settings) -> Database:
    database = Database(settings.db_path)
    yield database
    database.close()
