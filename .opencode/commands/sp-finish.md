---
description: Superpowers finishing-a-development-branch - verify tests then choose merge/PR/keep/discard
---

使用原生 `skill` 工具加载并执行 `finishing-a-development-branch` skill，然后处理下面的输入。

要求：

- 先确认测试与验证通过（`verification-before-completion`），再给出合并 / PR / 保留 / 丢弃选项。
- 不要擅自 push、合并或删除分支；需要用户确认。
- 与 OpenSpec 组合时，分支收尾前先完成 `opsx-verify` 与 `opsx-archive`。

输入：$ARGUMENTS