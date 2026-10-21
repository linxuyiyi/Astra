import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas import EmployeeCreate, EmployeePatch


@pytest.mark.parametrize("invalid", [-1, 9, None, 1.5, "2", True])
def test_character_rejects_invalid_create_and_patch(invalid):
    with pytest.raises(ValidationError):
        EmployeeCreate(id="invalid", name="无效", role="测试", character_id=invalid)
    with pytest.raises(ValidationError):
        EmployeePatch(character_id=invalid)


def test_employee_character_round_trip_and_dashboard():
    employee_id = "E-character"
    with TestClient(app) as client:
        created = client.post(
            "/api/agent-workers",
            json={
                "id": employee_id,
                "name": "形象测试员工",
                "role": "开发",
                "auto_claim": False,
                "category_ids": ["testing-platform"],
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["character_id"] == 0

        for character_id in range(9):
            updated = client.patch(f"/api/agent-workers/{employee_id}", json={"character_id": character_id})
            assert updated.status_code == 200
            assert updated.json()["character_id"] == character_id
            assert updated.json()["status"] == created.json()["status"]
            assert client.get(f"/api/agent-workers/{employee_id}").json()["character_id"] == character_id
            employees = client.get("/api/dashboard/agent-workers").json()
            item = next(
                item for team in employees["teams"] for item in team["employees"] if item["id"] == employee_id
            )
            assert item["character_id"] == character_id

        for invalid in (-1, 9, None, 1.5, "2", True):
            rejected = client.patch(f"/api/agent-workers/{employee_id}", json={"character_id": invalid})
            assert rejected.status_code == 422
            assert (
                client.post(
                    "/api/agent-workers",
                    json={
                        "id": "E-invalid-character",
                        "name": "非法形象",
                        "role": "开发",
                        "character_id": invalid,
                    },
                ).status_code
                == 422
            )
        patched = client.patch(f"/api/agent-workers/{employee_id}", json={"description": "只修改说明"})
        assert patched.json()["character_id"] == 8
        listed = next(item for item in client.get("/api/agent-workers").json() if item["id"] == employee_id)
        assert listed["character_id"] == 8

        second = client.post(
            "/api/agent-workers",
            json={
                "id": "E-character-second",
                "name": "同形象员工",
                "role": "开发",
                "character_id": 8,
                "category_ids": ["testing-platform"],
            },
        )
        assert second.status_code == 201
        assert second.json()["character_id"] == 8
        assert client.get(f"/api/agent-workers/{employee_id}").json()["character_id"] == 8
        client.delete("/api/agent-workers/E-character-second")
        client.delete(f"/api/agent-workers/{employee_id}")
