# -*- coding: utf-8 -*-
"""模块入口：``python -m nexus_backend``。

``scripts/services.py`` 会以 ``--instance-id <nonce>`` 启动本进程；该标识
随 ``/api/health`` 返回，供控制脚本确认监听的正是自己启动的实例。
"""
from __future__ import annotations

import argparse

import uvicorn

from .app import build_app
from .config import load_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="nexus_backend")
    parser.add_argument(
        "--instance-id",
        dest="instance_id",
        default=None,
        help="本次启动的随机实例标识，用于控制脚本精确归属校验。",
    )
    args, _ = parser.parse_known_args(argv)

    config = load_config()
    app = build_app(config, instance_id=args.instance_id)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
