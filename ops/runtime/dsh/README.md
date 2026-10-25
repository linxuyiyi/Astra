# DSH ACP Worker 与兼容性探针

本目录固定官方 DSH `0.2.1-rc.2`、Python ACP `0.8.1` 和完整 npm / Python 依赖。DSH 已接入 Worker Gateway、Task 生命周期、任务工作台和原生证据复盘；仅在显式配置 `runtime_engine=dsh` 的 Linux Worker 使用。默认引擎仍为 OpenCode，当前生产门禁尚未通过，实施与验收状态以 `openspec/changes/nexus-dsh-acp-runtime/tasks.md` 为准。

## 固定安装

使用独立 Python 3.11+ venv，在本目录执行 `npm ci`，再执行：

```bash
<venv>/bin/python -m pip install --require-hashes -r requirements.txt
```

必须运行 npm 安装脚本，不能使用 `--ignore-scripts`：DSH 有平台原生扩展，`dsh --version` 成功不能证明 ACP 能初始化。锁中版本与完整性不能因镜像源差异而修改。Worker wheel 的 `dsh` extra 包含固定 ACP / psutil；生成的 wheel 同时携带核验用 `dependency-lock.json`，不要求控制机安装 DSH 或 OpenCode。

Linux 预检遵循固定 Node 22 官方最低基线：x86_64 / ARM64、glibc 2.28+、kernel 4.18+；随后仍需通过依赖指纹、真实 ACP initialize 和 Gateway 目录检查。公司 ARM64/glibc 2.28、Windows 尚未取得实机证据。Windows 当前不启用 POSIX 进程监督。Mac 仅作独立探针使用。

上述是 Node / ACP 装配基线，不能当作完整 Worker 的最低要求。现有 Worker 文件通道使用 `openat2`（上游 Linux 5.6 引入），目标内核必须提供并允许该系统调用；DSH 健康检查通过真实文件通道核验，不采用路径隔离降级。原版 CentOS 8 的 4.18 内核不能满足完整 Worker。本次实测组合为 CentOS 8 / x86_64 / glibc 2.28、ELRepo 5.15.223 内核、独立 Python 3.11.15 和 Node 22.23.3；文件、Diff、停止与续接通过，不能据此声称其它发行版或安全策略已通过。

Bash 的 `workspace-write` 还需要可用的 bubblewrap 或 Landlock 后端。CentOS 8 补装 bubblewrap 后已取得默认沙箱下写入 / 读取自有标记、零审批绕过的实测证据；缺失后端时 DSH 拒绝不受约束地执行，不能将一次手工批准计为正常沙箱通过。该证据不替代独立系统身份与网络出口隔离验收。

完整离线归档、公司安装器与系统身份 / 网络隔离尚未验收。不要把此目录的锁文件或 Linux QA 通过当作离线部署完成。

## 显式配置 Worker

沿用现有 Worker 注册、WebSocket / HTTP 数据通道和工作区配置，另设置 `NEXUS_DSH_CONFIG` 指向当前 Worker 用户的私有 JSON 文件（0600）：

```json
{
  "executable": "/opt/nexus/dsh/node_modules/.bin/dsh",
  "lock_file": "/opt/nexus/dsh/package-lock.json",
  "state_root": "/var/lib/nexus-worker/dsh",
  "gateway_url": "https://nexus.example/v1",
  "key_file": "/etc/nexus-worker/gateway-key",
  "models": {
    "registered-route-alias": {
      "context_window": 131072,
      "max_tokens": 4096,
      "thinking_format": "deepseek"
    }
  },
  "skills": {},
  "mcp_servers": []
}
```

示例均为占位值，必须填写已启用的真实路由与稳定调用 key。模型没有默认厂商地址或自动候选回退。`key_file` 必须归 Worker 用户独占、0600，值不进入 argv、日志和仓库。`state_root` 为绝对路径、0700，不能和其他 Worker 共用；日志与原生磁盘应按组织保留策略管理。

Worker 管理页选择 DSH、Linux Remote、Worker 通道和明确路由别名。先在该 Linux Worker 独立安装固定运行时并配置 `NEXUS_DSH_CONFIG`，重启 Worker，执行“检查”；健康检查核对固定装配、真实文件通道、ACP、模型、Skill hash、MCP 名称与 Git 仓库，核验成功才允许调度。当前页面不提供 DSH 一键安装、更新或原生 Web 入口。

`skills` 是名称到本机 Skill 目录的精确白名单，`SKILL.md` 名称必须一致，拒绝 symlink。每绑定复制全部资源并冻结 hash，禁用 DSH 默认 Skill 根目录；恢复时发现漂移即拒绝。`mcp_servers` 使用官方 ACP 的固定 stdio / HTTP server 定义，控制面的 MCP 名称须与本地配置集合一致；不允许模型动态新增插件或 MCP。普通业务澄清使用已登记的 Task MCP，不能把工具审批当作业务回答。

每个绑定独立 DSH_HOME / Host / Git worktree，只保存最小绑定、受理 hash、generation 与 fence。prompt 在已受理后从 Nexus 待发送字段清除；消息历史只在原生磁盘。重复提交返回原 receipt，不重跑。断线只核对受理；Worker 重启后的在途提交保持 unknown，显式停止并证明归属进程组退出前保留占用。

正常 end_turn 只表示执行轮次结束，Task 仍等待人工确认。人工确认先回收 ACP Host，再使用既有业务完成 / 复盘流程；原生轨迹缺失只阻止成功复盘，不替代业务完成决策。工具审批仅允许本次允许 / 本次拒绝；超时、失联、未关联或跨 Session 均拒绝。

## 真实探针

通过安全环境配置 `NEXUS_GATEWAY_API_KEY`，从仓库 `services/nexus-api` 运行：

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

`--root` 的父目录必须存在、目标必须全新。`--protocol-only` 不运行推理，未运行项保持 UNVERIFIED。Qwen / GLM 按真实路由配置选择或省略 thinking format，不自动改模型。`--fault-probes` 仅对自有进程组和自有 Home 标记文件验证强制退出、同原生 Session 恢复、一次审批和取消。

报告只保存平台、源码 / 配置 / 依赖指纹、原生 ID、结果、轨迹摘要和原生用量。固定解析器只接受 v4 / plain JSONL，核对 workspace、连续 seq、文件完整性与所有轮次结束；拒绝缺失、symlink、乱序、部分尾行、读取中变化和未知版本。在线 context usage 不能当作计费用量。

退出码 1 表示至少一项失败，2 表示已运行项无失败但有未验证门禁；两者都不是生产通过。历次失败与后续复测分别留证，不覆盖失败报告。

## 安全与发布边界

Profile 禁用厂商账户、搜索、动态装配入口，只登记 Nexus Gateway。但相同系统用户仍可能读取自身凭据，DSH Bash 沙箱不承担网络出口隔离；部分 Linux 内核还可能报告 Landlock 部分生效。独立系统身份、网络白名单和公司实际工具策略必须另行验证。工具 / MCP 的本地白名单与 hash 不能替代 OS 隔离。

长 Session、限流、公司目标系统、Windows、GLM / Qwen 真实工具流和至少 100 任务 / 72 小时门禁仍未完成。不自动切默认引擎，不跨引擎重写已有 Session。

官方依据：[固定版 DSH ACP](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/packages/acp/acp/README.md)、[Gateway 配置](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/packages/llm/llm-pi-ai/README.md)、[ACP Python SDK](https://github.com/agentclientprotocol/python-sdk)、[Node 22 Linux 基线](https://github.com/nodejs/node/blob/v22.23.3/BUILDING.md)。
