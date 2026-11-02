# 新部署修复核验（2026-11-03）

## 原因与范围

基线为 `v1.4.9` / `a074809417ecc0b9e62cc17db9ff29eee79811ac`。公司提供的数据库、镜像及代理结论作为已验证事实输入。用户最新明确：这是尚未使用的新代码，不需要旧版兼容或假设性防御代码。

因此删除本轮额外添加的旧长标记 SQLite 转换脚本、归档 Worker / 无标记 OpenCode 识别交接模块、配套测试和新增旧网关参数自动清理。沿用已有单守护首次安装、受管启停和重复部署流程。上一轮归档进程交接结果不再代表交付功能。

## 修改内容

- Worker 连接平台后，还要通过 HTTP 领取任务内容、上传结果。补齐生成 Nginx 配置遗漏的 Data 路径，保持现有授权和正文限制。
- 只由默认 Library MCP 与员工显式绑定的 MCP 决定门禁；额外全局工具故障展示告警，不阻止正常员工接任务。
- 主服务、Worker 和 pip 隔离构建统一按已确认的公司 HTTP 来源安装。HTTPS 保留校验，不复制关闭 TLS 的临时方案。
- 平台一键部署生成同一 Nginx HTTP `/v1` 和 `/mcp` 地址，Worker 一键部署直接消费；聊天、embedding、ASR、复盘、Worker 与本机受管 OpenCode 共用模型入口。
- 采用唯一短 revision，不改现有迁移业务 DDL；恢复两份有效前端锁文件，发布仅执行冻结安装。

## 验证结果

脱敏数据见 [verification-results.json](verification-results.json)，原始日志位于本机 `/tmp/nexus-intranet-fix-20261102`。

撤销兼容代码后的针对性检查为 93 passed（18.88 秒），Ruff 通过。真实 Linux 已换用当前代码重启隔离控制面，再次一键部署 6.25 秒成功；最终 READY、可调度，守护与 OpenCode PID 不变，没有旧版交接阶段。

其余实现未变，沿用本轮已完成的验证：

| 检查 | 结果 |
| --- | --- |
| Web 单测 | 79 passed |
| 双前端空缓存 HTTP 镜像安装、type-check、build | chat-island 616 次、Web 210 次下载请求；其他源请求为 0，锁文件不变 |
| 实际 HTTP PyPI / PEP 517 构建 | 构建后端与产物同源获取，产物检查通过 |
| MySQL 8.0.36 严格模式 | 空库及现有迁移链检查通过，重复执行成功，业务数据保留 |
| 真 Nginx 组件 | TLS、WebSocket、SSE、ACL、Data URI/查询/认证/epoch 透传通过；8 MiB 和 28 MiB 正文成功，65 MiB 请求被 413 拒绝 |
| Linux 模型任务 | 经 Nginx 实际读取 README 并返回未写进提示词的证明值，确认后 SUCCEEDED |
| Linux Data | 20 MiB 文件大小及 SHA256 一致；8 MiB 指令正文成功 |
| 实际指令授权 | 匿名 401、跨 Worker 404、失效 epoch 409、HTTP 管理路径 404 |
| MCP 故障范围 | 额外故障仍 READY、可调度并显示告警；周期、手动检查、重启一致；测试配置已移除 |
| 浏览器 | 告警可见；列表逐项 health 请求为 0；同步 enabled PATCH 成功；无脚本错误 |

最初经 SSH 转发 Redis 的隔离夹具曾发生结果读取超时和短暂掉线。换用测试机同机独立 Redis 后，正文、文件、鉴权全部通过，连续三次原生结果读取成功（1.15、0.51、0.50 秒）。没有据此推定公司故障，没有新增产品重试兜底。

## /opsx-verify

调整后的两份 capability 共 7 条 Requirement、17 个 Scenario。实现与验证对应：

- revision：`sql_storage.py`、Alembic `0007`；fresh SQL 测试和真实严格 MySQL。
- 来源：`worker_deployment.py`、`remote_package_config.py`、安装器及远程安装；实际 pip/npm HTTP 测试。
- Nginx Data：配置生成、Nginx 组件与真实 Linux 正文、文件、授权检查。
- 冻结发布：两份 package-lock、`build_web`；非法锁/来源、锁冲突拒绝，以及两个完整项目的空缓存镜像构建。
- 模型入口：配置生成、Settings、model_route、opencode_model 和复盘客户端；统一地址、持久化及真实任务。
- MCP：逐服务探针、`worker_readiness.py` 与维护面板；readiness/equipment/maintenance 回归及真实故障。
- 保存与漂移：整体 spec_revision 比较继续保障手动同步及删除绑定；健康报告不重判在途任务终态。

完整性：tasks 11/11，7 条 Requirement、17 个 Scenario 均有实现与验证；正确性和设计一致性核对通过，无 CRITICAL 问题。未新增状态机、旧版识别模块、旧库转换或备用执行路径。OpenSpec validate 和 diff 检查通过；未归档 change，未修改 Astra 历史 specs。

## 剩余风险

未验证真实宿主机重启。Linux 控制面使用 SQLite，MySQL 严格模式另行验证；不代表公司现场 MySQL 与全部服务重新部署。真 Nginx 测试版本为 1.28.0，公司已验证的 1.12/TLS1.2 profile 另有配置测试。公司镜像协议由临时索引/registry 验证，未在公司网络重新下载。聊天、embedding、ASR、复盘由统一入口客户端测试覆盖，未重跑完整厂商在线验收。测试机运行工作区隔离快照；提交和推送状态以 Git 分支为准，不代表公司现场已升级。
