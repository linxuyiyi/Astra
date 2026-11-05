"""Task 与 Worker 展示使用其业务状态，不保存原生 Session 状态。"""

import json
from pathlib import Path

TASK_STATUS = json.loads(Path(__file__).with_name("task_status.json").read_text(encoding="utf-8"))
WORKER_LABELS = {"WORKING": "工作中", "IDLE": "空闲", "OFFLINE": "离线", "PAUSED": "已暂停"}


class RuntimeProjection:
    @property
    def runtime_state(self):
        return self.status

    @property
    def runtime_status_label(self):
        labels = TASK_STATUS if self.__tablename__ == "t_nexus_tasks" else WORKER_LABELS
        label = labels[self.status]
        return label["label"] if isinstance(label, dict) else label

    @property
    def runtime_detail(self):
        return getattr(self, "current_progress", "")
