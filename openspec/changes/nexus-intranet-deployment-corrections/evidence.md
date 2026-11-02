# 公司问题的独立核验

核验日期：2026-11-02。基线：`a074809417ecc0b9e62cc17db9ff29eee79811ac`。公司提供的问题结论、数据库状态和代码修改均为用户确认的真实、已验证事实。本文补充本仓库的源码定位与本地复现，用于设计回流修复，不重新质疑或要求用户证明公司结论。

## 逐项结论

| 问题 | 当前代码证据 | 判断及公司方案中的不足 |
| --- | --- | --- |
| revision 长度 | `services/nexus-api/app/sql_storage.py:13` 的当前标记长 33；`:14` 的版本列长 32；本环境 Alembic 默认版本表也为 VARCHAR(32) | 确定缺陷。严格 MySQL 空库和增量路径均受影响；SQLite 验收不能覆盖。采用公司已验证的短标记并同步版本常量；按用户明确的全新部署前提，不增加旧标记转换或公司库修复动作 |
| HTTP PyPI | `app/config/worker_deployment.py` 已接受合法 HTTP(S)；`app/runtime/remote_provisioning.py:1059` 从清单取 index，但命令清除信任配置且不补可信参数；`app/runtime/remote_package_config.py:33` 无差别拒绝已有 trusted-host | 配置接受与执行拒绝不一致。公司方向有依据，但只修 Worker 不够，`ops/deploy/deploy.py:802` 的主服务安装也要覆盖；必须检查端口、多值信任、所有 section 及隔离构建 |
| 旧进程 | `app/worker_agent/supervisor.py:97` 校验环境标识；`:188` 保留原生空闲门禁 | 未知进程拒绝规则正确。公司人工处理属于迁移运维，缺少 ENV 标识本身不构成安全清理证据 |
| MCP | `app/worker_agent/inspection.py:123` 默认把全部生效端点纳入 expected；`app/runtime/worker_readiness.py:713` 无范围区分地使用 missing/聚合 unhealthy；`:342` 仍将 drifted 生成配置阻断 | 额外全局 MCP 可污染调度。公司“degraded 改 drifted 后可调度”的结论不适用于本基线，直接改一行不能完成其声称行为，且会混淆配置与故障 |
| 镜像配置 | Worker 常量采用公共华为云源；`ops/deploy/sources/huawei.json` 的域名为 `mirrors.tools.huawei.com`；用户已确认遮蔽字段为 `huawei`，且公司 npm/PyPI 使用 HTTP | 地址已确认。保留该公司 profile，按记录修正 npm/PyPI 协议；公共源继续独立，不能用家中公网不可达推断公司源无效 |
| 代理 | `ops/deploy/deploy.py:210` 已有独立受 ACL 限制的 Worker HTTP server；`:265` 仅连接路由，未配置 Data；`app/worker_agent/local_opencode.py:18` 从 WebSocket origin 导出 Data 地址 | 确定缺少正式入口上的 Data 路由。另开一个通用 HTTP /v1/ 没有修正同 origin 的 Data 缺失，也可能绕开已有 ACL |
| npm ci | `.gitignore:48`、`:49` 忽略两个锁文件；两个文件均未被 Git 跟踪；`ops/deploy/deploy.py:891` 允许无锁时 npm install | 当前不具备直接强制 ci 的前提。需要连同锁文件、版本树比对、镜像映射和构建验证一起交付 |
| Nginx/TLS | `ops/deploy/sources/versions.json:6` 为最低 1.20；`ops/deploy/deploy.py:252` 保留 TLS1.2/1.3 | 公司给出的兼容修改作为已验证输入，通过显式公司 profile 表达，不全局改公共默认值；测试本次代码生成的配置与其一致 |

上表 `app/` 路径均以 `services/nexus-api/` 为根。行号是此基线的定位信息。

## 用户补充的确定信息

2026-11-02，用户确认文档中的遮蔽字段替换为 `huawei` 即为真实镜像地址。具体四类 URL 与协议已写入 design.md。该确认解决地址输入问题；本轮没有据此声称已从公司目标网络完成下载验证。

## 本轮只读复现输出

使用当前 `.venv/bin/python` 读取源码定义、编译 MySQL 版本表 DDL，并调用实际 MCP 装备判定函数：

```text
revision_length=33
app_version_width=32
alembic_version_width=32
MySQL Alembic DDL: version_num VARCHAR(32) NOT NULL

输入：唯一必需项 nexus-library 的实际端点正确；
      missing 仅包含未受管 unmanaged-project-mcp。
当前 MCP 装备结果：
state=degraded, verified=False, configured=True
差异=MCP 连接异常：unmanaged-project-mcp

HTTP source_url('http://mirror.test:8080/simple') 已被接受。
Git 跟踪的 web/chat-island package-lock.json 数量为 0。
```

这些复现补充说明本仓库的长度矛盾、额外 MCP 范围污染和配置/执行契约缺口。公司原始报错及处理结论沿用用户已提供的验证结果。

## 官方依据

- [Alembic version_table_impl](https://alembic.sqlalchemy.org/en/latest/api/ddl.html#alembic.ddl.impl.DefaultImpl.version_table_impl)：版本表是 Alembic 元数据；本轮安装版本的实现已在本地核实为 String(32)。不采用不存在的列宽配置项或修改私有对象。
- [pip 命令选项](https://pip.pypa.io/en/stable/cli/pip/)：trusted-host 支持主机或 host:port，也会放宽该来源的 HTTPS 验证，因此不能把 HTTPS 源一并加入。
- [pip 配置规则](https://github.com/pypa/pip/blob/main/docs/html/topics/configuration.md)：可信主机可多值，命令 section 可覆盖 global；只检查单值或 global 不足。
- [pip 证书验证](https://pip.pypa.io/en/stable/topics/https-certificates/)：提供 CA bundle 是处理企业证书的正式方式之一，不需要关闭验证。
- [npm ci](https://docs.npmjs.com/cli/commands/npm-ci/)：要求现有且一致的锁文件，不更新依赖声明或锁文件。
- [npm registry host 替换](https://docs.npmjs.com/cli/v10/using-npm/config/#replace-registry-host)：npm 支持把默认 registry 的锁定制品映射到配置源，保留锁与选择镜像可以同时实现。
- [Nginx TLS 配置](https://nginx.org/en/docs/http/ngx_http_ssl_module.html#ssl_protocols)：TLS1.3 能力还依赖 OpenSSL，不能只根据 Nginx 版本数字判断。
- [Nginx proxy_pass](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)：是否携带 URI 会改变转发路径；Data 验收必须核对实际 URI。

## 验证边界

本轮只读复现和方案文档不等于新增实现完成。公司现场的已验证结论直接接受；上一轮本地 Linux/SQLite 报告仅用于描述本地代码测试，不能替代本次新增实现的回归。实施以本 change 的 `tasks.md` 为唯一清单，不额外设置用户补充现场材料的前置条件。
