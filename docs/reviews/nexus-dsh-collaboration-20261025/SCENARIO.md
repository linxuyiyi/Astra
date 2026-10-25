# DSH 数字员工协作闭环复验

基线为主线 efc85a6ea 与 DSH 适配 b7f4d70f1 的隔离组合，分支 codex/dsh-collaboration-e2e。原两条 QA 栈和远端 Worker 保持不动。

A 通过真实 contact_employee 询问 B，B 通过工具交还，A 通过真实 Task MCP 建单给 Linux agentwork 的 DSH Worker。任务在全新 worktree 先检索读取已发布知识，再红测、修复、绿测，由外部 SSH 独立验收；待确认与已完成状态自动回原群，页面人工确认后观察非影子复盘，员工通过新总结工具读取真实状态。后续 B 在另一个 Worker 和新工作区检验经验复用。

关键断言：引擎必须 dsh，DSH 0.2.1-rc.2 / ACP 0.8.1，原生 UUID 与平台 binding 独立，不允许 OpenCode 回退；真实 MCP search/get 与源知识 ID 可核对；原生消息数不能用事件数替代；验收前 WAITING 保持占用，验收后归零。

执行数据是固定格式时间的事件汇总；覆盖乱序最大时间、同时间最后输入双向、跨 run/跨 case、空输入和互异时间六种排列。只使用本轮隔离 QA 仓库，不 push 任务代码，不自动发布候选经验。对本轮复盘候选独立核验后，仅在 QA 目录发布一条、驳回一条；第三个全新 DSH 任务必须检索读取本轮新经验并实施，单独证明新增经验复用。既有历史知识的真实复用与本轮新增候选分开记录；NO_FINDINGS 不伪造经验。

本轮为功能短测，不能替代公司安装器、MySQL、不同模型、长 Session 或 72 小时生产稳定性门禁。
