"""OpenCode 观测 helper。

OpenCode 进程由Nexus 通过 SSH管理，本类不拉起、不重启进程，只记录观测结果。
通信断开是网络故障，健康状态由 Worker 本地只读探测给出；断线绝不触发
OpenCode restart。
"""


class RuntimeSupervisor:
    def __init__(self) -> None:
        self.restart_count = 0

    def on_connection_lost(self) -> None:
        return

    def on_process_exit(self, _code: int) -> None:
        self.restart_count += 1
