## Context

复核基线 11621869f。MySQL 初始化已完成，本变更补齐跨进程环境与模型调用契约，不改 schema。

## Goals / Non-Goals

**Goals:** 修复四项 P1；embeddings 复用共享网关；更新文档。**Non-Goals:** 不管理外部 llm_wiki 的服务器部署和模型配置，不改变 AgentScope 事实源或任务状态机，不为历史库增加兼容。

## Decisions

1. 安装器生成受保护、可由 shell 与 systemd 共同加载的环境文件。由稳定生成/复用的内部网关 key 经部署期显式登记数据库摘要，不把密钥写到日志或命令参数。不在安装器自动创建指定厂商节点；节点与模型路由由网关控制面维护。
2. chat 与 embedding 共享同一数据面请求管线，仅区分上游路径、流式能力及最小协议校验。嵌入节点提供独立轻量探测策略，不以 chat/SSE 探测误判 embedding-only 节点。
本机受管 Serve 通过 OpenCode 内联配置限定 `enabled_providers=[nexus-gateway]` 并绑定主/小模型；不写用户全局文件，旧 Serve 不符合时拒绝复用；远程受管部署不保留按测试模型名跳过路由或 key 的特殊入口。依据 [OpenCode 官方配置说明](https://docs.opencode.ai/docs/config/)。
3. 知识库嵌入配置改为 model_route_id、dimensions，由现有员工模型路由模块确保网关 credential，保持 AgentScope 官方知识库和向量实现。
4. Worker 地址统一规范化 /v1；非法子路径直接失败。llm_wiki 明确已有 token/project_id 并执行已存在的鉴权项目检索接口；外部模型端点不是 Nexus 的远程管理接口，不冒用旧 Web runtime 配置协议。
5. README 和 AGENTS.md 明确远程模式、两类数据库、Redis、网关模型归属与外部服务配置边界。

## Risks / Trade-offs

- 公司 Linux/systemd、实际厂商和外部 llm_wiki 需现场验收；本地用干净子进程、模拟上游及真实 MySQL 验证可控制边界。
- 现有任意厂商 embedding credential 配置将被拒绝，需替换为网关路由配置。未配置 embedding 时维持关闭。
