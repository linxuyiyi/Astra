## Why

公司内网部署记录暴露出已发布 revision 超过版本表长度、HTTP 镜像的配置与执行规则不一致、全局额外 MCP 污染调度门禁，以及受限 Worker 入口遗漏 Data 路由。本仓库上一轮验收没有覆盖真实 Nginx 入口和严格 MySQL，这些问题需要按实际调用链修复，而不是直接合入公司排障补丁。

## What Changes

- 采用公司已验证的短 revision `0007_worker_readiness`，同步当前版本常量，并增加所有 revision 不超过 32 字符的回归检查。公司已完成的数据库处理沿用；本仓库按新代码、全新部署处理，不增加旧标记转换、扩列或双标记兼容。
- 保留公网默认源；公司实际域名已由用户确认为 `mirrors.tools.huawei.com`，通过现有 huawei source profile 提供，npm/PyPI 按公司配置使用 HTTP，Python/Node 使用 HTTPS。保留显式 `NEXUS_REMOTE_*` 覆盖，补齐两条 pip 路径的 HTTP 同源信任，HTTPS 保留证书校验；不关闭 npm strict-ssl。
- MCP 调度只依赖平台要求及管理员显式绑定的受管 MCP；额外全局 MCP 异常保留诊断告警，不能使已满足要求的 Worker 不可调度。配置漂移与连接故障继续使用原有语义，不把所有 MCP 失败改成 DRIFTED。
- 平台、AgentScope 和 Worker 的模型调用统一使用平台自动生成的 Nginx HTTP `/v1` 地址；复盘同样从该地址派生，不保留内部直连网关或独立客户端地址。
- 在现有 ACL 限制的 Worker HTTP 入口补齐 Data 路由；公司已提供的 Nginx/TLS 兼容配置通过显式部署 profile 表达，公共默认配置独立，不新增通用 HTTP 入口。
- **BREAKING**：主前端与聊天岛恢复提交有效锁文件，发布构建必须使用 npm ci；缺锁或不一致直接失败。锁定版本与完整性，按受控规则映射至配置镜像，不再现场解析依赖。
- 按用户最新明确的全新部署前提，仅使用现有单守护的安装、启停与重复部署流程，不增加旧版进程识别或交接实现。

## Capabilities

### New Capabilities

- `nexus-intranet-deployment`: 受控镜像、数据库版本元数据、可复现构建、Worker 入口与一键部署的契约。
- `nexus-worker-mcp-health-scope`: 受管 MCP 与额外全局 MCP 的健康范围、调度门禁和告警语义。

### Modified Capabilities

无。`openspec/specs/` 中现存内容属于 Astra 历史契约，不修改；本提案延续 `nexus-worker-deploy-health-convergence` 的单一就绪事实源与手动同步规则。

## Impact

影响 `services/nexus-api` 的迁移入口、数据库版本定义、安装源治理、Worker 探针、就绪判定与诊断展示，以及 `ops/deploy` 的安装、Nginx 配置和前端依赖锁文件。没有新增业务表、任务能力需求系统、运行时兼容分支或 systemd 依赖；不复制 AgentScope 状态，不导入早期数据库。

公司提供的问题、数据库处理及代码修复均作为已经验证的事实输入，不再次索要证明。本轮交付针对本仓库的修复方案、契约和实施清单；新增实现通过本仓库回归验证，不将未提供的新故障或泛化修复工具加入范围。
