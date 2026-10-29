"""验证一次性回填保留旧办公室外观，不碰任务或员工身份字段。"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa


def test_avatar_backfill_preserves_previous_office_faces_and_saved_choices(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0006_employee_avatars.py"
    spec = importlib.util.spec_from_file_location("avatar_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    # 预期值由变更前 v1.4.9 的 robotVariant 实际输出记录，包含非 BMP 字符 ID。
    examples = [
        ("emp-auto-0", "测试自动化工程师", "robot-v1-1"),
        ("emp-auto-1", "自动化", "robot-v1-0"),
        ("emp-system", "测试系统工程师", "robot-v1-2"),
        ("emp-ops", "防线运营人员", "robot-v1-5"),
        ("generic", "其他", "robot-v1-3"),
        ("员工🚀", "其他", "robot-v1-0"),
    ]
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE t_nexus_employees (id TEXT PRIMARY KEY, domain TEXT, "
                "description TEXT, avatar TEXT)"
            )
        )
        connection.execute(sa.text("CREATE TABLE t_nexus_tasks (id TEXT, status TEXT)"))
        connection.execute(sa.text("INSERT INTO t_nexus_tasks VALUES ('task', 'RUNNING')"))
        for employee_id, domain, _expected in examples:
            connection.execute(
                sa.text("INSERT INTO t_nexus_employees VALUES (:id,:domain,'','🤖')"),
                {"id": employee_id, "domain": domain},
            )
        connection.execute(
            sa.text("INSERT INTO t_nexus_employees VALUES ('saved','旧职责','','botpixel-v1-beret')")
        )
        monkeypatch.setattr(migration, "op", SimpleNamespace(get_bind=lambda: connection))
        migration.upgrade()
        migration.upgrade()
        rows = {
            row[0]: (row[1], row[2])
            for row in connection.execute(sa.text("SELECT id, domain, avatar FROM t_nexus_employees"))
        }
        for employee_id, domain, expected in examples:
            assert rows[employee_id] == (domain, expected)
        assert rows["saved"] == ("旧职责", "botpixel-v1-beret")
        assert connection.execute(sa.text("SELECT status FROM t_nexus_tasks")).scalar_one() == "RUNNING"
    engine.dispose()
