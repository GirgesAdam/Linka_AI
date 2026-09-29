from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root() -> None:
    response = client.get("/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "Linka"
    assert payload["status"] == "running"


def test_liveness() -> None:
    response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "alive"
    assert payload["app"] == "Linka"


def test_request_id_is_returned() -> None:
    response = client.get(
        "/api/v1/health/live",
        headers={"x-request-id": "test-request-id"},
    )
    assert response.headers["x-request-id"] == "test-request-id"
