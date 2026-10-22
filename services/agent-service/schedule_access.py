"""通过官方访问策略向人类日程身份共享平台资源，日程和 Session 仍按人隔离。"""

from agentscope.app.access import ResourceAccessPolicyBase, ResourceKind, ResourceRef

SCHEDULE_USER_PREFIX = "nexus-schedule-"


class ScheduleResourceAccessPolicy(ResourceAccessPolicyBase):
    def __init__(self, owner_id: str):
        self.owner_id = owner_id

    async def list_accessible(self, viewer_id, kind, storage):
        suffix = viewer_id.removeprefix(SCHEDULE_USER_PREFIX)
        if (
            not viewer_id.startswith(SCHEDULE_USER_PREFIX)
            or len(suffix) != 64
            or any(c not in "0123456789abcdef" for c in suffix)
        ):
            return []
        loaders = {
            ResourceKind.AGENT: storage.list_agents,
            ResourceKind.CREDENTIAL: storage.list_credentials,
            ResourceKind.KNOWLEDGE_BASE: storage.list_knowledge_bases,
        }
        loader = loaders.get(kind)
        if loader is None:
            return []
        return [
            ResourceRef(kind=kind, owner_id=self.owner_id, resource_id=record.id)
            for record in await loader(self.owner_id)
        ]
