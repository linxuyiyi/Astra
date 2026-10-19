# Nexus 部署指南

生产安装、升级、回退、健康检查和排障以 [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md) 为准。

- 唯一生产入口：`ops/deploy/install_nexus.sh`。
- 配置模板：`ops/env/nexus-production.env.example`。
- Nginx 模板由部署生成；静态参考见 `ops/deploy/nginx-nexus.conf`。
- 开发态仍使用 `ops/env/.env.example`、`ops/dev/start_astra.sh` 中的命令与 `ops/dev/astra_service.py dev`。
- `ops/dev/astra_service.py pro` 不再托管生产进程、迁移或构建；它会提示使用生产 CLI。
- 真实 Linux systemd/Nginx/MySQL 验收使用 `ops/deploy/tests/linux_smoke.sh`，未在目标主机执行前不得标记为通过。
