---
description: Superpowers planning - turn an agreed change into one executable task list
---

使用原生 `skill` 工具按需加载 `writing-plans`，处理输入：$ARGUMENTS

- 先判断是否已有 OpenSpec change；如有，读取该 change 的 proposal / specs / design / tasks.md，只补足缺失的可执行步骤，不输出第二份计划。
- 任务按可独立验证的交付单元拆解；不要把每次工具调用、文件阅读或提交拆成任务。
- 不涉及 Spec 的局部修改，直接给出简短改动与验证办法，不强制写计划文档。
- 计划中的路径、接口和测试须来自已读到的真实代码，不写未确认的技术假设。
