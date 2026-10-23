"""DSH Worker 命令名的单一事实源；基础 Worker 导入时不加载可选 SDK。"""

COMMANDS = frozenset(
    {
        "dsh_prepare",
        "dsh_submit",
        "dsh_cancel",
        "dsh_permission",
        "dsh_snapshot",
        "dsh_trace",
        "dsh_health",
        "dsh_lookup",
        "dsh_dispose",
    }
)
MUTATIONS = frozenset({"dsh_prepare", "dsh_submit", "dsh_cancel", "dsh_permission", "dsh_dispose"})
