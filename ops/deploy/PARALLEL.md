# 与 v0.9 同机部署

Nexus 与 v0.9 可以装在同一台机器上，各自使用下面这组名字。部署前在仓库根目录执行：

```bash
python ops/deploy/check_parallel.py
```

脚本只读取本仓库的默认值和部署脚本里的路径，不启动 v0.9，也不在同一台机器上做并行验收。有重叠时退出码为 1。

和 v0.9 共用同一个 OS 用户时，v0.9 的用户全局 OpenCode 配置会叠加进 Nexus。推荐每个产品使用专用的 OS 用户。不要改 `XDG_CONFIG_HOME`。

| 项 | v0.9 | Nexus |
|---|---|---|
| 数据库名 / 用户 | `astra` / `astra` | `nexus` / `nexus` |
| SQLite 文件 | `astra.db` | `nexus.db` |
| Redis | 库 `0`，前缀 `astra:` | 库 `1`，前缀 `nexus:` |
| Celery 队列 | `control`（任务名 `astra.*`） | `nexus-control`（任务名 `nexus.*`） |
| systemd | `astra-opencode-{id}.service`、`astra-worker-{id}.service` | `nexus-opencode-{id}.service`、`nexus-worker-{id}.service` |
| 路径 | `/opt/astra`、`/etc/astra` | `/opt/nexus`、`/etc/nexus` |
| 锁目录 | `/tmp/astra-employee-serve` | `/tmp/nexus-employee-serve` |
| 部署缓存 | `~/.cache/astra-provision` | `~/.cache/nexus-provision` |
| OpenCode 配置 | 用户全局 `~/.config/opencode/opencode.jsonc` | `/etc/nexus/opencode/{id}/opencode.jsonc`（`OPENCODE_CONFIG`） |
| 端口 | 8000、8100、8110、8120、8201、8202、5174、4096、4097 | 18000、18110、18120、18201、18202、18203、14096 |

Redis 进程仍监听 6379。`ASTRA_REDIS_URL` 使用库 1。`ASTRA_*` 变量名保持不变。

Celery 的 `agent` 队列名，以及 worker 主机名 `astra-control`、`astra-agent`，沿用原名。它们和 v0.9 不在同一个 Redis 库里。
