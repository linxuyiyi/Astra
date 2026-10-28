from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "test_system_server"

DEMO_VERSIONS = [
    {
        "product_name": "NR 测试套件",
        "version": "v1.2.0",
        "status": "RELEASED",
        "release_date": "2026-06-01",
        "description": "当前稳定版本",
        "is_default": True,
    },
    {
        "product_name": "NR 测试套件",
        "version": "v1.1.0",
        "status": "RELEASED",
        "release_date": "2026-03-01",
        "description": "上一稳定版本",
        "is_default": False,
    },
    {
        "product_name": "NR 测试套件",
        "version": "v2.0.0-beta",
        "status": "DRAFT",
        "release_date": None,
        "description": "开发中的下一版本",
        "is_default": False,
    },
    {
        "product_name": "NR 测试套件",
        "version": "v0.9.0",
        "status": "ARCHIVED",
        "release_date": "2025-12-01",
        "description": "已归档的历史版本",
        "is_default": False,
    },
    {
        "product_name": "其他产品",
        "version": "v3.1.0",
        "status": "RELEASED",
        "release_date": "2026-08-01",
        "description": "其他产品的稳定版本",
        "is_default": True,
    },
]


def main() -> None:
    sys.path.insert(0, str(SERVER_DIR))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
    os.environ.setdefault("DATABASE_ENGINE", "sqlite")
    os.environ.setdefault("SQLITE_PATH", str(REPO_ROOT / ".runtime" / "demo.sqlite3"))

    import django

    django.setup()

    from apps.versions.models import ProductVersion
    from apps.versions.services import normalize_key

    for spec in DEMO_VERSIONS:
        product_key = normalize_key(spec["product_name"])
        version_key = normalize_key(spec["version"])
        ProductVersion.objects.update_or_create(
            product_key=product_key,
            version_key=version_key,
            defaults={
                "product_name": spec["product_name"],
                "version": spec["version"],
                "status": spec["status"],
                "release_date": spec["release_date"],
                "description": spec["description"],
                "is_default": spec["is_default"],
                "default_key": product_key if spec["is_default"] else None,
            },
        )

    username = (os.getenv("NR_DEMO_USERNAME") or "").strip()
    password = os.getenv("NR_DEMO_PASSWORD") or ""
    if username and password:
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        user, created = user_model.objects.get_or_create(
            username=username,
            defaults={"email": f"{username}@example.com", "is_staff": True, "is_superuser": True},
        )
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        user.set_password(password)
        user.save()
        print(f"demo user {'created' if created else 'updated'}: {username}")

    print(f"seeded {len(DEMO_VERSIONS)} demo versions into {os.environ['SQLITE_PATH']}")


if __name__ == "__main__":
    main()
