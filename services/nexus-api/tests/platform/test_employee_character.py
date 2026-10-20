import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import MetaData, Table, create_engine, inspect, select

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
        created = client.post("/api/agent-workers", json={
            "id": employee_id, "name": "形象测试员工", "role": "开发", "auto_claim": False,
            "category_ids": ["testing-platform"],
        })
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
                item for team in employees["teams"] for item in team["employees"]
                if item["id"] == employee_id
            )
            assert item["character_id"] == character_id

        for invalid in (-1, 9, None, 1.5, "2", True):
            rejected = client.patch(f"/api/agent-workers/{employee_id}", json={"character_id": invalid})
            assert rejected.status_code == 422
            assert client.post("/api/agent-workers", json={
                "id": "E-invalid-character", "name": "非法形象", "role": "开发", "character_id": invalid,
            }).status_code == 422
        patched = client.patch(f"/api/agent-workers/{employee_id}", json={"description": "只修改说明"})
        assert patched.json()["character_id"] == 8
        listed = next(item for item in client.get("/api/agent-workers").json() if item["id"] == employee_id)
        assert listed["character_id"] == 8

        second = client.post("/api/agent-workers", json={
            "id": "E-character-second", "name": "同形象员工", "role": "开发", "character_id": 8,
            "category_ids": ["testing-platform"],
        })
        assert second.status_code == 201
        assert second.json()["character_id"] == 8
        assert client.get(f"/api/agent-workers/{employee_id}").json()["character_id"] == 8
        client.delete("/api/agent-workers/E-character-second")
        client.delete(f"/api/agent-workers/{employee_id}")


def test_character_migration_preserves_home_characters_and_can_downgrade(tmp_path):
    database = tmp_path / "characters.db"
    environment = {
        **os.environ,
        "ASTRA_DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}",
        "ASTRA_ENVIRONMENT": "development",
        "ASTRA_SEED_DEMO": "false",
    }

    def migrate(command, revision):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", command, revision],
            cwd=Path(__file__).resolve().parents[2], env=environment,
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("upgrade", "0032_gateway_saved_config")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    # 期望值来自旧首页 JS 表达式，覆盖中文与补充平面字符。
    expected = {"astra-01": 3, "astra-02": 4, "员工甲": 2, "employee-😀": 8}
    try:
        with engine.begin() as connection:
            employees = Table("digital_employees", MetaData(), autoload_with=connection)
            for employee_id in expected:
                connection.execute(employees.insert().values(
                    id=employee_id, name="保留员工", role="开发", description="保留说明",
                    status="PAUSED", task_types=["ISSUE"], category_ids=[], enabled=True,
                    auto_claim=False, auto_execute=False, current_task_count=0,
                    created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
                ))
            before = [dict(row) for row in connection.execute(select(employees)).mappings()]
        migrate("upgrade", "0033_employee_character")
        with engine.connect() as connection:
            employees = Table("digital_employees", MetaData(), autoload_with=connection)
            rows = [dict(row) for row in connection.execute(select(employees)).mappings()]
            assert {row["id"]: row.pop("character_id") for row in rows} == expected
            assert rows == before
            column = next(
                column for column in inspect(connection).get_columns("digital_employees")
                if column["name"] == "character_id"
            )
            assert column["nullable"] is False
            assert str(column["default"]).strip("'") == "0"
        migrate("downgrade", "0032_gateway_saved_config")
        with engine.connect() as connection:
            employees = Table("digital_employees", MetaData(), autoload_with=connection)
            assert "character_id" not in employees.c
            assert [dict(row) for row in connection.execute(select(employees)).mappings()] == before
    finally:
        engine.dispose()
