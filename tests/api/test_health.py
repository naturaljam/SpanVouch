from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spanvouch.api.app import create_app


def test_health_returns_service_identity() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "spanvouch",
    }


def test_api_public_identity() -> None:
    application = create_app()

    assert application.title == "SpanVouch"
    assert application.version == "0.7.0"


def test_old_database_environment_variable_does_not_override_new_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPANVOUCH_DB_PATH", raising=False)
    monkeypatch.setenv("AF" + "C_DB_PATH", str(tmp_path / "old.db"))

    with TestClient(create_app()):
        pass

    assert (tmp_path / ".data" / "spanvouch.db").is_file()
    assert not (tmp_path / "old.db").exists()


def test_new_database_environment_variable_selects_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "configured" / "spanvouch.db"
    monkeypatch.setenv("SPANVOUCH_DB_PATH", str(database))

    with TestClient(create_app()):
        pass

    assert database.is_file()
