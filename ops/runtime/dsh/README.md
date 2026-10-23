# DSH ACP P0 兼容性探针

当前是 `nexus-dsh-acp-runtime` 的第一阶段基础设施，**不是已接入任务大厅的 DSH 执行引擎**。正式派单、持久受理账本、Runtime migration、工作台、复盘和灰度仍以该 change 的 `tasks.md` 为唯一清单。P0 未通过，不切换默认引擎。

## 固定版本与安装

- 官方 DSH：`@deepseek-ai/dsh@0.2.1-rc.2` / tag `dsh-v0.2.1-rc.2`。
- 官方 Python ACP：`agent-client-protocol==0.8.1`；启用其标准 `session/resume` 扩展。官方 DSH 的 `agentInfo.version=0.0.1` 不能作为包版本证据。
- `package-lock.json` 固定完整 npm 依赖及完整性；`requirements.txt` 固定探针 venv 依赖及 SHA256。Python 使用独立 **3.11+** 环境；当前 Nexus 服务本身要求 3.11+。
- Mac Node 25.4.0 与 Linux Node 22.23.3 已实测；公司 ARM64/glibc 2.28、Windows 仍需实机门禁。Windows Session 文件锁与进程监督当前明确拒绝运行。

在本目录执行 `npm ci`，不要从 `/tmp` 别名路径以 `npm --prefix` 生成锁文件。镜像源可以按公司网络指定，但不能修改锁中版本或绕过完整性。创建独立 venv 后执行：

```bash
<venv>/bin/python -m pip install --require-hashes -r requirements.txt
```

`pip install 'services/nexus-api[dsh]'` 只增加固定 ACP 客户端；完整离线归档、安装器装配、安全网络策略和生产健康检查仍属于 P1。

## 运行

先通过本机安全环境配置 `NEXUS_GATEWAY_API_KEY`，值必须是 Nexus 已登记的 Gateway 调用 key。探针不读取厂商 key，不提供默认厂商地址，不把 key 放进命令参数或报告。例：从仓库 `services/nexus-api` 执行：

```bash
<venv>/bin/python ../../tools/e2e/dsh_acp_probe.py \
  --dsh <runtime>/node_modules/.bin/dsh \
  --root <private-parent>/new-probe \
  --gateway-url https://nexus.example/v1 \
  --model <已登记路由别名> \
  --context-window <明确配置的上下文上限> \
  --max-tokens <明确配置的输出上限> \
  --thinking-format deepseek \
  --fault-probes
```

`--root` 的父目录必须已存在，目标目录必须全新。`--protocol-only` 不运行推理，模型/MCP/取消等项保持 `UNVERIFIED`。Qwen 或非 thinking 路由按实际契约省略或修改 `--thinking-format`；不因为模型失败自动换路由或引擎。

探针核对安装依赖指纹、真实网关目录、原生模型选择、双轮、MCP fixture、重启恢复、取消、持久轨迹。`--fault-probes` 在自有进程组做 SIGKILL，再打开同一 Session 并确认没有自动重投；随后用精确的自有标记命令测试拒绝和 allow-once。官方将 `/tmp` 列入 `workspace-write` 白名单，所以审批标记创建在探针用户的独立 Home 子目录，结束后清理。

固定版权限请求只有 `toolCallId`，监督器从同 Session 的先行 generic tool lifecycle 关联命令详情；未关联、过期、超时或跨 Session 一律拒绝。临时强制停止检查整组退出，不以本地 future 消失或父进程退出表示成功。当前代码未接入正式 Worker Gateway，也没有宣称跨进程持久去重已完成。

## 证据与退出码

`report.json` 只保存平台、版本/配置/源码指纹、原生 Session ID、测试结果、事件类型计数、轨迹摘要和原生用量。全文仍留在独立 `DSH_HOME`，不复制到 Nexus 数据表或 Git。轨迹解析器仅支持本 Profile 明确启用的 v4/plain JSONL；缺失、链接、乱序、未结束轮次或格式不匹配不能作为完整结果。ACP context usage 与原生 input/output/cache token 分开记录，不能从前者推算计费用量。

- `1`：至少一项失败，包括清理不能证明成功。
- `2`：已运行项没有失败，但完整门禁仍有未验证项。
- 不将 `INCOMPLETE` 当作正式通过；长 Session、限流、公司/Windows 实机以及目标 agentwork 的门禁不能用单次短测代替。

Profile 禁用了厂商模型/账户、网络搜索、动态插件装配等入口，并只装配显式 Gateway 别名。但同系统用户仍可能读取本地凭据文件，DSH Bash 文件沙箱也不负责网络白名单。独立系统身份、MCP/网络白名单和稳定持久提交属于正式接入前的 P1 门禁；本探针不宣称已经提供这些隔离。

官方依据：[DSH ACP 固定 tag](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/packages/acp/acp/README.md)、[DSH pi-ai Gateway 配置](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/packages/llm/llm-pi-ai/README.md)、[官方 ACP Python 客户端](https://github.com/agentclientprotocol/python-sdk)。
