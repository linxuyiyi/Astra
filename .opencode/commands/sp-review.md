---
description: Superpowers requesting-code-review - review a change range against its requirements
---

使用原生 `skill` 工具加载并执行 `requesting-code-review` skill，然后处理下面的输入。

要求：

- 明确 BASE..HEAD 范围（无参数时用 `git merge-base` 或会话上下文推断）。
- 使用项目内 `code-reviewer` 子代理执行只读评审，不在主会话里自行改代码。
- 按 Critical / Important / Minor 汇报，并给出 spec 合规与质量两个结论。

输入：$ARGUMENTS