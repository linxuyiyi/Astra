# 第一阶段审查记录

本记录是验证结果，不是第二份实施清单。所有实施状态只更新 `openspec/changes/nexus-v1-0-1/tasks.md`。

## 原因与范围

按用户要求从空业务仓库建设 Nexus v1.0.1，仅复制 Astra 的工程规范与工作流。新仓库为 `/Users/niuniu/程序/Nexus`，分支 `codex/nexus-v1.0.1`；来源 Astra 分支 `rewrite/astra-1.0`、提交 `157456988721c18d71c7c2583a893e3f81f6119b`。来源仓库原有未跟踪 `docs/Astra_1.0/`，本次未修改。

## 改动

- 复制通用 OpenSpec/Superpowers skill、命令、schema、CLI bundle、插件和相关检查；最小适配项目规则，不复制业务代码、历史 Spec、数据、平台助手和 MCP。
- 保留用户需求全文和 75,703 字节最终 HTML 原型，副本与来源 `cmp` 一致。
- 建立 `nexus-v1-0-1` change：proposal、design、六项 capability spec 和唯一 tasks 清单。
- 官方能力研究与 UI 验收映射分别在 `AGENTSCOPE_RESEARCH.md`、`UI_ACCEPTANCE.md`；代码开发使用 OpenCode `deepseek/deepseek-flash`，由用户确认。DeepSeek 官方说明该 API 名称当前对应 V4.1 Flash。

## 实际验证

- `node .opencode/tools/openspec.mjs templates --json`：四项均使用项目 schema。
- `node scripts/check_openspec_config.mjs`：配置有效，四组规则。
- `node tests/opencode/test-superpowers-bootstrap.mjs`：默认及 `NEXUS_SUPERPOWERS_BOOTSTRAP=1` 两种环境均通过。独立审查发现的测试隔离问题和 V2 根目录保护缺失已由 OpenCode 修复，Codex 重新执行回归确认。V2 无法确认目录时保守不注入；skill 注册保留。
- `node .opencode/tools/openspec.mjs validate nexus-v1-0-1 --strict`：六项 capability 产物通过。
- 安装正式 `agentscope[service,storage-sql]==2.0.10` + `aiosqlite`，使用 Python 3.12 隔离环境、官方 create_app、ASGI lifespan、真实 SQLite 创建 Agent/Session。关闭并重建服务实例后读取同一 Agent/Session 成功。实际 OpenAPI 保存在 `/tmp/nexus-native-probe-g660e80j/openapi.json`，临时数据库不作为交付数据库。
- 实测发现顶层 AgentRecord `id` 不同于 `data.id`，Session 列表是 SessionView 包装结构；设计与规格已覆盖。
- 再次检查来源 Astra `git status --short --branch`，保持原分支和原未跟踪文件。

## 风险与未验证项

上述 SQLite 验证是官方 ASGI 生命周期重建，不是完整部署、真实进程重启或真实模型联调。员工业务、页面、附件、多员工通信均尚未实现，后续不得将 OpenSpec 规划产物完整误报为功能完成。官方 Team 不等于平等讨论；公开 peer 扩展方案须以真实三员工消息链验收。

官方参考：[AgentScope 2.0.10](https://pypi.org/project/agentscope/2.0.10/)、[固定 tag 源码](https://github.com/agentscope-ai/agentscope/tree/v2.0.10)、[DeepSeek V4.1 Flash API 名称](https://deepseek.com/news/deepseek-v4-1-flash/)。
