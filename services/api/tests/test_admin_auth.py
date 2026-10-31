from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_post_with_evil_origin_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "ENFORCE_ADMIN_ORIGIN", True)
    res = client.post(
        "/api/v1/automation/pause",
        headers={"Origin": "https://evil.example"},
    )
    assert res.status_code == 403
    assert res.json() == {"detail": "Origin not allowed"}


def test_post_with_allowed_origin_succeeds(client, monkeypatch):
    monkeypatch.setattr(settings, "ENFORCE_ADMIN_ORIGIN", True)
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN", "")
    res = client.post(
        "/api/v1/automation/pause",
        headers={"Origin": "http://localhost:3000"},
    )
    assert res.status_code != 403


def test_post_with_sec_fetch_site_cross_site_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "ENFORCE_ADMIN_ORIGIN", True)
    res = client.post(
        "/api/v1/automation/pause",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert res.status_code == 403
    assert res.json() == {"detail": "Cross-site request blocked"}


def test_post_with_no_origin_and_no_sec_fetch_site_allowed(client, monkeypatch):
    monkeypatch.setattr(settings, "ENFORCE_ADMIN_ORIGIN", True)
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN", "")
    res = client.post("/api/v1/automation/pause")
    assert res.status_code != 403


def test_admin_token_enforcement(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN", "super-secret-admin-token")
    # Missing token
    res_missing = client.post("/api/v1/automation/pause")
    assert res_missing.status_code == 403
    assert res_missing.json() == {"detail": "Admin token required/invalid"}

    # Incorrect token
    res_wrong = client.post(
        "/api/v1/automation/pause",
        headers={"X-Admin-Token": "wrong-token"},
    )
    assert res_wrong.status_code == 403
    assert res_wrong.json() == {"detail": "Admin token required/invalid"}

    # Correct token
    res_ok = client.post(
        "/api/v1/automation/pause",
        headers={"X-Admin-Token": "super-secret-admin-token"},
    )
    assert res_ok.status_code != 403


def test_get_with_evil_origin_not_blocked(client, monkeypatch):
    monkeypatch.setattr(settings, "ENFORCE_ADMIN_ORIGIN", True)
    res = client.get(
        "/api/v1/automation/status",
        headers={"Origin": "https://evil.example"},
    )
    assert res.status_code == 200


def test_evil_host_header_rejected(client):
    res = client.get(
        "/api/v1/automation/status",
        headers={"Host": "evil.example"},
    )
    assert res.status_code == 403
    assert res.json() == {"detail": "Host not allowed"}


def test_testserver_host_allowed(client):
    res = client.get(
        "/api/v1/automation/status",
        headers={"Host": "testserver"},
    )
    assert res.status_code == 200

    res_local = client.get(
        "/api/v1/automation/status",
        headers={"Host": "testserver.local:8000"},
    )
    assert res_local.status_code == 200


def test_run_cycle_requires_json_body_and_rejects_non_object(client):
    # Non-object payload (e.g. array or primitive or invalid string)
    res_array = client.post("/api/v1/automation/run-cycle", json=[1, 2, 3])
    assert res_array.status_code == 422

    res_primitive = client.post("/api/v1/automation/run-cycle", content="not-a-json", headers={"Content-Type": "application/json"})
    assert res_primitive.status_code == 422

    # Valid JSON object should be accepted
    res_valid = client.post(
        "/api/v1/automation/run-cycle",
        json={"simulate_ai_invalid_json": False, "confirm": True},
    )
    assert res_valid.status_code != 422
