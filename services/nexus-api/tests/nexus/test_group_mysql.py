"""真实 MySQL 8.0.36 并发契约；仅在显式测试数据库 URL 下运行。"""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from nexus_api.collaboration import append_message, lock_channel
from nexus_api.db import connect
from nexus_api.main import create_app

pytestmark = pytest.mark.skipif(not os.environ.get("NEXUS_TEST_MYSQL_URL"), reason="需要显式隔离 MySQL URL")


@pytest.fixture(autouse=True)
def isolated_sql_database(monkeypatch):
    from app.config.settings import get_settings
    from app.sql_storage import create_test_schema

    raw = os.environ.get("NEXUS_TEST_MYSQL_URL")
    if not raw:
        yield
        return
    url = make_url(raw)
    name = "nexus_group_test_" + uuid4().hex[:12]
    admin = create_engine(url.set(drivername="mysql+pymysql", database=None))
    with admin.begin() as db:
        assert db.execute(text("SELECT VERSION()")).scalar_one().startswith("8.0.36")
        db.execute(text(f"CREATE DATABASE {name} CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"))
    target = url.set(database=name, drivername="mysql+asyncmy")
    monkeypatch.setenv("ASTRA_DATABASE_URL", str(target))
    get_settings.cache_clear()
    create_test_schema(target)
    try:
        yield
    finally:
        get_settings.cache_clear()
        with admin.begin() as db:
            db.execute(text(f"DROP DATABASE {name}"))
        admin.dispose()


def account(client, name):
    client.cookies.set("nexus_uid", name)
    assert client.get("/api/v1/me").status_code == 200


def test_one_use_invitation_cannot_join_two_humans():
    app = create_app()
    with TestClient(app) as owner:
        account(owner, "mysql-owner")
        group = owner.post("/api/v1/channels", json={"name": "MySQL协作"}).json()["id"]
        token = owner.post(f"/api/v1/channels/{group}/invitations").json()["token"]
        others = [TestClient(app), TestClient(app)]
        for i, client in enumerate(others):
            account(client, f"mysql-other-{i}")
        barrier = threading.Barrier(2)

        def join(client):
            barrier.wait()
            return client.post("/api/v1/channels/join", json={"token": token}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(join, others))
        assert sorted(codes) == [200, 410]
        assert len(owner.get(f"/api/v1/channels/{group}/members").json()["members"]) == 2


def test_group_event_cursor_follows_commit_order():
    app = create_app()
    with TestClient(app) as owner:
        account(owner, "mysql-owner")
        cid = owner.post("/api/v1/channels", json={"name": "MySQL顺序"}).json()["id"]
        started, release, second_finished = threading.Event(), threading.Event(), threading.Event()

        def first():
            with connect() as db:
                lock_channel(db, cid)
                append_message(db, cid, "human", "first", "甲", "第一条")
                started.set()
                assert release.wait(3)
                db.commit()

        def second():
            assert started.wait(3)
            with connect() as db:
                lock_channel(db, cid)
                append_message(db, cid, "human", "second", "乙", "第二条")
                db.commit()
                second_finished.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(first), pool.submit(second)
            assert started.wait(3)
            assert not second_finished.wait(0.1)
            release.set()
            a.result()
            b.result()
        with connect() as db:
            payloads = [
                json.loads(p)
                for p in db.execute(
                    text("SELECT payload_json FROM t_nexus_group_events WHERE channel_id=:c ORDER BY id"),
                    {"c": cid},
                ).scalars()
            ]
        messages = [
            p["item"]["text"]
            for p in payloads
            if p["type"] == "message" and p["item"]["sender_type"] == "human"
        ]
        assert messages == ["第一条", "第二条"]


def test_group_history_and_pagination_with_event_cursor():
    with TestClient(create_app()) as owner:
        account(owner, "mysql-owner")
        cid = owner.post("/api/v1/channels", json={"name": "MySQL历史"}).json()["id"]
        paths = [f"/api/v1/channels/{cid}/{suffix}" for suffix in (
            "messages", "timeline", "timeline/native",
        )]
        for path in paths:
            response = owner.get(path)
            assert response.status_code == 200
            assert all(item["sender_type"] == "system" for item in response.json()["items"])
            assert isinstance(response.json()["event_cursor"], int)
        initial_cursor = response.json()["event_cursor"]
        initial_items = response.json()["items"]
        with connect() as db:
            lock_channel(db, cid)
            append_message(db, cid, "human", "first", "甲", "第一条")
            append_message(db, cid, "human", "second", "乙", "第二条")
            db.commit()
        for path in paths:
            latest = owner.get(path, params={"limit": 1}).json()
            assert [item["text"] for item in latest["items"]] == ["第二条"]
            assert latest["event_cursor"] > initial_cursor
            earlier = owner.get(path, params={"limit": 1, "before": latest["before"]}).json()
            assert [item["text"] for item in earlier["items"]] == ["第一条"]
            assert earlier["event_cursor"] == latest["event_cursor"]
            original = owner.get(path, params={"before": earlier["before"]}).json()
            assert original["items"] == initial_items
            assert original["before"] == 0
