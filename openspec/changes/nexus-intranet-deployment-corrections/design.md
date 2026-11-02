## Context

核验基线为 `v1.4.9` 的 `a074809417ecc0b9e62cc17db9ff29eee79811ac`。公司提供的排障结论、数据库处理、代码修改和环境信息均作为已经验证的事实输入。本地代码检查用于确定正确的回流实现，不能把公司结论重新列为未知项，也不整体照搬临时补丁。代码证据及本轮只读复现见 [evidence.md](evidence.md)。

当前链路仍为 `Task → Run → Employee → Worker → OpenCode Session`。节点观测经网关上报，就绪与调度门禁由 `worker_readiness.py` 统一判定。AgentScope 的 Agent、Session、Memory 不受此次变更影响。

## Goals / Non-Goals

**Goals:** 同时覆盖干净部署和本分支已发布 schema 的增量部署；让显式配置的内网源可执行；保持调度状态含义一致；验证经过正式 Nginx 入口的真实 Worker 通信；使同一发布依赖可复现。

**Non-Goals:** 不自动导入早期数据库，不修改 `0001`–`0006` 或 `0007` 的业务 DDL；不扩宽版本表，不要求公司把已修好的短标记改回长标记；不把公司配置固化为公共默认值；不增加任务能力编排或一套健康状态机；不自动接管未知进程；不新增通用明文管理入口。

## Decisions

### 1. 采用已验证的短 revision，修正本仓库的一致性

公司已经完成 `0007` DDL，并将版本标记修正为 `0007_worker_readiness`。这些是已确认事实，本次不再要求公司重新证明或重做数据库处理。

本仓库将 `0007` 的 revision 元数据和 `CURRENT_REVISION` 统一为该短标记，保留 down_revision、业务 DDL 和 `0001`–`0006`。增加所有 revision 长度不超过现有 32 字符容量的检查，并验证空 MySQL、0006 增量与公司已完成短标记的重复升级路径。

用户已明确这是尚未使用的新代码，本次按全新部署处理。删除旧长标记 SQLite 的转换脚本和对应测试，不增加旧版本配置清理或双标记读取。保留统一短标记及空库初始化验证，不修改既有迁移业务 DDL。

### 2. 公司镜像配置化，HTTP 信任按明确来源收窄

用户已确认：记录中的遮蔽字段替换为 `huawei` 即为实际镜像地址，地址与协议不再属于待确认事项。

| 制品 | 公司实际配置 |
| --- | --- |
| Python 源码 | `https://mirrors.tools.huawei.com/python/3.11.17/Python-3.11.17.tar.xz` |
| Node.js | `https://mirrors.tools.huawei.com/nodejs` |
| npm registry | `http://mirrors.tools.huawei.com/npm/` |
| PyPI index | `http://mirrors.tools.huawei.com/pypi/simple` |

公网默认源继续保留。现有 `ops/deploy/sources/huawei.json` 作为公司镜像 profile，实施时按上述协议修正 npm/PyPI；不能因该域名在家中公网不可达，就将此 profile 改成公共华为云地址。控制机和 Worker 消费解析后的清单，已有显式 `NEXUS_REMOTE_*` 覆盖优先级继续生效。地址已知与目标机实际可达是两件事，后者由部署预检核验。

两条 pip 安装路径必须一起修：`ops/deploy` 的主服务/Agent Service 安装，以及 `remote_provisioning.py` 的远端 Worker wheel 安装。

- 先校验完整 HTTP(S) URL，拒绝空 hostname、凭据、无效端口、查询片段等不合约输入；不根据 `startswith` 拼可信参数。
- HTTPS 不加 `trusted-host`，保留证书验证。公司 CA 必须按实际客户端的信任机制配置并验证，不能用 `strict-ssl=false`、`curl -k` 或 `verify=False` 代替。
- 显式配置 HTTP PyPI 时，安装命令只为该来源生成精确的可信主机参数；如配置了非默认端口，必须包含端口，不能扩大到整个主机。
- 本次安装继续忽略外部 pip 配置和额外源；隔离构建子进程也使用同一镜像及同一授权。不能只修直接安装命令而使 PEP 517 构建依赖退回公网。
- 检查既有 `PIP_TRUSTED_HOST`、全部 pip 配置 section 及多值项：HTTP 仅允许与本次来源完全一致的值；HTTPS 下该 TLS 绕过项仍拒绝。`extra-index-url`、`find-links`、不同 index 等冲突一律拒绝且不覆盖。
- 写入现有约定的镜像默认配置时使用 ConfigParser，完整预检后再备份、原子写入，重复部署不得生成重复 section 或重复信任项。HTTP 的配置检查、持久化结果与安装命令遵守同一规则。
- npm 使用 HTTP registry 不需要关闭 HTTPS 校验；保留 `strict-ssl=true`，并检查配置文件和环境变量没有暗中关闭它。

这里承认 HTTP 不提供传输保密与服务端身份验证；精确 `trusted-host` 是功能授权边界，不能把它描述为 HTTPS 的等价替代。Python/OpenCode 已有校验和策略保留，HTTP 元数据本身仍有来源风险。

### 3. 修 MCP 健康范围，不改变 DRIFTED 的含义

当前周期探针默认把生效配置中的全部 MCP 纳入 expected 集合；随后就绪判定直接接受聚合失败，造成未绑定的项目工具污染平台调度。这一点已用当前代码独立复现。

本次最小契约：

| 对象 | 配置/健康问题 | 展示与调度 |
| --- | --- | --- |
| `nexus-library`、管理员在 `mcp_refs` 显式绑定的 MCP | 地址或绑定未同步 | DRIFTED，禁止新派单，提示手动一键部署 |
| 上述受管 MCP | 已生效但连接失败 | DEGRADED，禁止新派单，展示具体服务错误 |
| 上述受管 MCP | 状态不可读取、快照缺少逐服务证据 | UNKNOWN，禁止新派单，不推测健康 |
| OpenCode 全局/项目自行配置、未被平台绑定的 MCP | 缺失、未连接或配置异常 | 保留逐项诊断告警；必需项全部健康时可 READY、可调度 |

Worker 只报告逐服务观测和实际端点，不决定哪些服务必需。控制面以当前 Desired 中的受管集合计算门禁；手动检查、周期心跳、重启后的报告使用同一判定。快照增加逐服务连接事实，状态汇总不得只凭聚合 unhealthy 字段判断所有受管项失败。

本次不新增 `required=false`、任务能力依赖表或前端策略编辑器。当前明确绑定本就表示平台要求；公司给出的五个工具属于额外全局配置，按该已确认范围处理告警。若以后需要“显式绑定但可选”，应另定契约，不能本轮默认所有 MCP 可选。

诊断中保留额外失败的名称和安全错误摘要，前端展示为告警；不把真实故障伪装成配置漂移。真实配置变化依然阻止新任务，保存依然不自动同步，在途任务不因此被重判终态。

### 4. 补齐现有 Worker 入口，保持完整鉴权链

已有 `NEXUS_DEPLOY_WORKER_PORT` 是绑定指定 IP、带 CIDR allow/deny 的 Worker HTTP 入口，已提供 `/v1/`、`/mcp`、WebSocket connect。无需再开 80 端口 `/v1/` 来解决相同可达性问题。

在该 server 补充 `/api/worker-gateway/data/` 到统一 API 的代理，保留原 URI、认证头、epoch、查询参数和错误码；该路径沿用同一 ACL，不代理其他管理 `/api` 路径，未知路径仍 404。沿用现有流式、请求体和超时设置，并按真实协议上限验证：指令正文 8 MiB，文件 20 MiB（含编码与封装开销），不为了通过测试无限放大。

Data 的实际授权由 API 校验 token、Worker 归属、request_id 与 epoch。Nginx 不能注入服务端共享身份或把匿名请求变成有效请求。测试既要通过合法传输，也要拒绝匿名、跨 Worker 和过期 epoch。

公司已提供的 Nginx 最低 1.12、TLS1.2 兼容修改作为明确配置输入，通过公司部署 profile 生成；公共默认 profile 保持现有要求。用本仓库的配置生成和集成测试验证实现符合该输入，不再要求用户确认现场版本或补证书材料。管理 HTTPS 与受限 Worker 内网入口继续分离，不能通过 HTTP 镜像配置顺带关闭 HTTPS 验证。

### 5. 锁版本、换受控下载源

强制 npm ci 的方向正确，但当前 web、chat-island 的锁文件均未被 Git 跟踪且被忽略。只改命令会把部署变成必然失败。

恢复提交两份与 package.json 一致的锁文件，然后将发布安装收敛为 `npm ci --ignore-scripts`。缺锁、无效锁、依赖声明不一致、非允许制品来源直接失败；不回退 npm install，不在生产重新解析版本。

锁文件保护版本树和 integrity，镜像 profile 控制下载位置。对默认 npm registry 的制品使用 npm 已有的 registry host 替换能力；额外自定义 registry 仍需显式允许，不能把任意 URL 全部重写后当成安全来源。用记录请求的临时 registry 加公网不可达环境证明实际只访问指定源，而不是只检查命令字符串。原始锁内容和 integrity 不随公司部署改写。

保持显式构建脚本和现有编译步骤；验证 ignore-scripts 下所需构建依赖齐全。AgentScope 独立 UI 的 pnpm frozen-lockfile 流程不改成另一套 npm 流程。

### 6. 使用现有新部署与重复部署流程

用户最新明确不需要旧版兼容。撤销本轮新增的归档 Worker、无环境标记 OpenCode 识别和交接逻辑；继续使用已有单守护、部署锁、受管启停和幂等部署流程。新部署不扫描或转换旧版进程记录，不增加备用部署路径。

### 7. 所有模型调用统一经过 Nginx HTTP

平台一键部署只计算一份 `http://<控制机IP>:<Nginx内网入口端口>/v1`，默认端口为 18130。将该地址持久化为唯一的 `NEXUS_GATEWAY_OPENAI_BASE`，平台聊天、AgentScope credential、embedding、ASR、Worker OpenCode 和本机开发 OpenCode 都读取它。复盘接口从该基址附加 `/chat/completions`，不再提供独立 URL。

网关进程继续监听回环 18110，仅作为 Nginx upstream。删除客户端对 `127.0.0.1:18110/v1` 的隐式默认，以及独立 `NEXUS_REMOTE_GATEWAY_LOCAL_URL`/复盘 URL 配置入口；不增加旧环境变量的自动清理或兼容读取。地址和 Nginx 配置共同由平台一键部署生成，Worker 一键部署直接消费同一地址，管理员不用维护两份值。

## Risks / Trade-offs

- 正式服务仅使用唯一短标记；按新代码部署，不提供旧长标记转换。
- 额外 MCP 不阻断调度不代表其功能可用 → 保留告警；显式受管能力仍严格核验；暂不承诺任务级能力匹配。
- 锁文件恢复可能暴露依赖树差异 → 比对现有可用环境和正式版本，先完成双前端测试与构建再发布。
- 明文内网入口/镜像无法消除窃听或篡改风险 → 限定在部署方确认的受信任网段；支持可信 HTTPS，不以 TLS 绕过解决证书问题。
- 公司结论与本仓库新增实现的验证要区分 → 接受公司提供的已验证事实；本地测试证明本次实现与这些事实及目标行为一致，不将测试限制描述成公司现场的新问题。

## Migration Plan

按 `tasks.md` 实施并完成本仓库针对性回归、MySQL 和代理集成测试。公司已有短标记和完成的 DDL 直接沿用，不要求重新修改数据库。不提供旧长标记或旧版进程兼容。部署源和代理兼容性按已给出的公司 profile 生成；受管配置或绑定变更继续由管理员手动部署。

## 已确认输入

公司提供的结论、数据库信息和代码修改均真实且已验证；镜像域名与协议已明确。没有要求用户再次证明的现场前置项。实现过程中若发现本仓库代码有额外缺陷，必须先给出实际代码或复现证据，再决定是否属于本次范围。
