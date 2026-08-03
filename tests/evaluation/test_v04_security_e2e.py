from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spanvouch.adapters.storage import sqlite_schema
from spanvouch.adapters.storage.sqlite_schema import connect_database, initialize_database
from spanvouch.api.app import create_app
from spanvouch.audit.chain import AuditChain, AuditEvent
from spanvouch.audit.export import verify_audit_export
from spanvouch.projects.repository import ProjectRepository
from spanvouch.security.identity import Role

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _create_v3_database(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        for statement in sqlite_schema._SCHEMA_SQL.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_metadata(singleton_key, schema_version) VALUES (1, 3)"
        )
        connection.execute(
            "INSERT INTO traces(trace_id, run_id, trace_json, trace_sha256) "
            "VALUES ('legacy-trace', 'legacy-run', '{}', ?)",
            ("a" * 64,),
        )
        connection.execute(
            "INSERT INTO review_cases("
            "case_id, status, version, verification_mode, diagnoser, "
            "current_revision_number, evidence_revision_count, created_at, updated_at"
            ") VALUES ('legacy-case', 'pending_verification', 0, 'deterministic', "
            "'rules', 0, 0, '2026-07-31T00:00:00Z', '2026-07-31T00:00:00Z')"
        )


def _write_signing_key(path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _create_admin_key(database: Path) -> str:
    repository = ProjectRepository(database)
    _, plaintext = repository.create_key(
        None,
        (Role.ADMIN,),
        now=NOW,
        expires_at=None,
    )
    return plaintext


def _app(database: Path) -> FastAPI:
    application = create_app(
        review_database=database,
        project_repository=ProjectRepository(database),
    )
    application.state.clock = lambda: NOW
    return application


@contextmanager
def _client(database: Path) -> Iterator[TestClient]:
    with TestClient(_app(database)) as client:
        yield client


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _trace_payload(trace_id: str) -> dict[str, Any]:
    started_at = datetime(2026, 7, 31, 0, 0, tzinfo=UTC).isoformat()
    return {
        "schema_name": "spanvouch.trace",
        "schema_version": "1.0",
        "trace_id": trace_id,
        "run_id": f"run-{trace_id}",
        "spans": [
            {
                "trace_id": trace_id,
                "span_id": "root",
                "parent_span_id": None,
                "name": "supportlab.run",
                "kind": "agent",
                "status": "ok",
                "started_at": started_at,
                "ended_at": started_at,
                "attributes": {},
            }
        ],
    }


def _backup_database(source: Path, destination: Path) -> None:
    with (
        sqlite3.connect(source) as source_connection,
        sqlite3.connect(destination) as destination_connection,
    ):
        source_connection.backup(destination_connection)


def _project_audit_events(database: Path, project_id: str) -> tuple[AuditEvent, ...]:
    with connect_database(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM audit_events WHERE project_id = ? ORDER BY event_sequence",
            (project_id,),
        ).fetchall()
    return tuple(AuditEvent.from_row(row) for row in rows)


def test_v3_migration_backup_restore_revocation_and_signed_export_survive_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "legacy-v3.sqlite3"
    _create_v3_database(database)
    initialize_database(database)
    signing_key_path = tmp_path / "audit-signing-key.pem"
    _write_signing_key(signing_key_path)
    monkeypatch.setenv("SPANVOUCH_AUDIT_SIGNING_KEY_PATH", str(signing_key_path))
    monkeypatch.setenv("SPANVOUCH_AUDIT_EXPORT_DIR", str(tmp_path / "exports"))
    admin_key = _create_admin_key(database)

    with connect_database(database) as connection:
        assert connection.execute(
            "SELECT project_id FROM traces WHERE trace_id = 'legacy-trace'"
        ).fetchone() == ("default",)
        assert connection.execute(
            "SELECT project_id FROM review_cases WHERE case_id = 'legacy-case'"
        ).fetchone() == ("default",)

    with _client(database) as client:
        assert client.app.version == "0.7.0"
        project_response = client.post(
            "/v1/admin/projects",
            json={"name": "Recovered Alpha"},
            headers=_auth(admin_key),
        )
        assert project_response.status_code == 201
        project_id = project_response.json()["project_id"]
        key_response = client.post(
            f"/v1/admin/projects/{project_id}/api-keys",
            json={"roles": ["operator"], "expires_at": None},
            headers=_auth(admin_key),
        )
        assert key_response.status_code == 201
        project_key = key_response.json()["api_key"]
        key_id = key_response.json()["key_id"]
        assert (
            client.post(
                "/v1/traces",
                json=_trace_payload("recovered-trace"),
                headers=_auth(project_key),
            ).status_code
            == 201
        )
        assert (
            client.post(
                f"/v1/admin/api-keys/{key_id}/revoke",
                headers=_auth(admin_key),
            ).status_code
            == 204
        )
        export_response = client.post(
            f"/v1/admin/projects/{project_id}/audit-exports",
            headers=_auth(admin_key),
        )
        assert export_response.status_code == 201
        bundle = Path(export_response.json()["bundle_path"])

    backup = tmp_path / "backup.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    _backup_database(database, backup)
    shutil.copy2(backup, restored)

    with _client(restored) as client:
        assert (
            client.post(
                "/v1/traces",
                json=_trace_payload("revoked-key-after-restore"),
                headers=_auth(project_key),
            ).status_code
            == 401
        )
        listed_exports = client.get("/v1/admin/audit-exports", headers=_auth(admin_key))
        assert listed_exports.status_code == 200
        assert [item["bundle_path"] for item in listed_exports.json()["exports"]] == [
            str(bundle)
        ]

    verified = verify_audit_export(bundle)
    assert verified.manifest.package.version == "0.7.0"
    assert verified.project_id == project_id
    assert verified.event_count >= 4
    AuditChain().verify(_project_audit_events(restored, project_id))


def test_app_restart_preserves_project_permissions_and_audit_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "spanvouch.sqlite3"
    initialize_database(database)
    signing_key_path = tmp_path / "audit-signing-key.pem"
    _write_signing_key(signing_key_path)
    monkeypatch.setenv("SPANVOUCH_AUDIT_SIGNING_KEY_PATH", str(signing_key_path))
    monkeypatch.setenv("SPANVOUCH_AUDIT_EXPORT_DIR", str(tmp_path / "exports"))
    admin_key = _create_admin_key(database)

    with _client(database) as first_client:
        project_response = first_client.post(
            "/v1/admin/projects",
            json={"name": "Restart Alpha"},
            headers=_auth(admin_key),
        )
        assert project_response.status_code == 201
        project_id = project_response.json()["project_id"]
        key_response = first_client.post(
            f"/v1/admin/projects/{project_id}/api-keys",
            json={"roles": ["operator"], "expires_at": None},
            headers=_auth(admin_key),
        )
        assert key_response.status_code == 201
        project_key = key_response.json()["api_key"]
        assert (
            first_client.post(
                "/v1/traces",
                json=_trace_payload("before-restart"),
                headers=_auth(project_key),
            ).status_code
            == 201
        )

    with _client(database) as restarted_client:
        assert restarted_client.app.version == "0.7.0"
        assert (
            restarted_client.post(
                "/v1/traces",
                json=_trace_payload("after-restart"),
                headers=_auth(project_key),
            ).status_code
            == 201
        )
        export_response = restarted_client.post(
            f"/v1/admin/projects/{project_id}/audit-exports",
            headers=_auth(admin_key),
        )
        assert export_response.status_code == 201

    verified = verify_audit_export(Path(export_response.json()["bundle_path"]))
    assert [event.action for event in verified.events].count("trace.ingest") == 2
    assert verified.manifest.package.version == "0.7.0"
    AuditChain().verify(_project_audit_events(database, project_id))
