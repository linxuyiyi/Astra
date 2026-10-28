# MCP 版本查询验收：业务默认值先确认，协议结果与 HTTP 对照

## 适用场景

为已有测试系统补充只读 MCP 查询能力，且默认查询范围由业务人员决定。默认策略与认证、查询语义和演示数据准备需要分别验证。

## 可复用方法

1. **先确认业务默认值。** Worker 通过当前原生问题提出选择；责任数字员工读取求助，在原私聊说明选项与建议。用户答复后，员工重新确认 Task、Session 与请求仍一致，再回传；不要求人进入任务工作区操作。
2. **默认值与显式参数分开测试。** 本轮用户选择默认仅查询 `RELEASED`；通过 `NR_DEFAULT_STATUSES` 可覆盖，显式 `status` 参数优先。默认单状态筛选下推现有 HTTP API，结果中的 `client_side_filtering=false`；日期范围或多状态聚合需要客户端处理时才标记为 `true`，不能将该标志写成统一固定值。分页抓取达到上限时应显式失败，避免把部分结果作为完整结果。
3. **单测、静态检查、格式检查分开列证据。** `pytest`、`ruff check`、`ruff format --check` 分别运行；依赖与命令使用同一虚拟环境解释器。mock 单测通过不等于真实服务验证通过。
4. **真实 MCP 验证经过协议边界。** 启动临时本地 Django 服务，通过 stdio MCP 客户端完成握手、ListTools 与 CallTool。核对默认状态、日期筛选与分页、UUID 详情；将搜索和详情结构与直接 HTTP 调用逐项对照。
5. **认证和演示初始化分离。** MCP 服务只从环境变量读取 Cookie、只发 GET；演示验证脚本的登录与数据种子是测试夹具能力，不是 MCP 写能力。独立复跑的夹具口令只经进程环境传入，不写入源码或报告；Worker 演示阶段曾使用临时口令文件，测后删除，不能泛称所有凭据从不落盘。日志局部计数为 0 不能单独证明全系统无泄露。
6. **确认完成与资源释放都要核对。** 交付待验收时在原私聊报告真实测试结果和提交；验收后由员工确认任务，再分别核对任务完成、Worker 占用为 0 和临时服务退出。

## 本轮验证证据

- 任务：`T-20261028-2298D2`；原生 Session：`ses_e7d09fe61ffefYMD8ds6QB4aEz`。
- 原生业务问题：`que_182f66a0b001OFEcn39RYJfyQO`。真实用户选择 A，仅已发布；员工 `nexus_task_reply` 返回 `accepted=true`，命中同一请求与 Session。
- 提交：`747c96e` 与 `ea85e34ecc2e3cdaeb3a9122f24fb3fbfaac60f4`；13 个新增文件位于 `mcp_server/`，未由 Worker 推送远端。
- 独立复跑：**27 passed**；Ruff 检查通过；格式检查 12 个文件通过。
- 独立真实协议验证：`2025-11-25`；发现 `search_product_versions`、`get_product_version`。默认仅已发布、日期筛选与分页均通过，搜索与详情结果等同直接 HTTP 调用。
- 临时 Django 服务经进程身份核对后关闭；任务成功，Worker 为空闲、占用为 0。
- 可复核产物：`human-answer-native-ack.json`、`independent-live-mcp.json`、`independent-test.txt`、`independent-ruff.txt`、`independent-format.txt`、`final-proof.json`。

## 边界与审核修订

本轮为已有 Linux 隔离环境、SQLite 与自建演示数据；没有验证全新空环境安装、公司 MySQL 或生产网络。原始候选把 `client_side_filtering=true` 泛化为全部查询，并将中途缺依赖的失败当作最终可重复性结论，本次依据最终代码及独立复跑修正。执行通道曾断连，技术恢复后仍为同一 Task、Session 和原请求；不能宣称全程无故障。
