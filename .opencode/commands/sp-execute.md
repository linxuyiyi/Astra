---
description: Superpowers execution - implement approved tasks with proportionate tests
---

使用原生 `skill` 工具按需加载 `executing-plans`，处理输入：$ARGUMENTS

- 有 OpenSpec change 时，以该 change 的 `tasks.md` 为唯一任务清单；完成后更新勾选并执行 `/opsx-verify`。不要再生成第二份计划。
- 复杂且相互独立的任务才调用 `subagent-driven-development`；小修复直接内联实现，不为每个任务派发子代理与重复评审。
- 新增或改变业务行为时优先 TDD；文档、配置、纯样式或纯重构改动运行对应检查即可，不生成无意义的测试。
- 每个交付单元核对实际修改与测试输出。没有真实证据，不报告通过。
