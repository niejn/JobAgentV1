"""Journey creation tool; execution is gated by framework HITL, not a model flag."""

from pathlib import Path

from langchain_core.tools import StructuredTool

from jobagent.journey.creation import CreateJourneyRequest, create_journey


def build_create_journey_tool(path: Path):
    async def create_opportunity_journey(**kwargs):
        journey, created = create_journey(path, CreateJourneyRequest.model_validate(kwargs))
        return {
            "status": "deleted" if journey.deleted_at else "created" if created else "existing",
            "journey_id": journey.id,
            "company": journey.company,
            "role": journey.role,
            "stage": journey.stage,
            "jd_missing": not bool(journey.job_description),
            "message": ("该 Journey 已软删除，需要单独审批恢复，不能报告网页列表可见。" if journey.deleted_at else
                        "Journey 已保存，可在网页求职旅程列表查看；这不表示已投递或已发送消息。"),
        }

    return StructuredTool.from_function(
        coroutine=create_opportunity_journey,
        name="create_opportunity_journey",
        description=("用户关注、收藏、要求跟进或投递具体岗位时，调用此工具请求人工批准创建网页 Journey。"
                     "公司和岗位必填；已知 boss:<id> 等稳定岗位 ID 时传 source_job_id。"
                     "JD 未取得时留空，不得编造。此操作会单独暂停等待批准，与打招呼/投递批准独立。"
                     "拒绝后不要反复请求。仅返回 journey_id 才能声称 Journey 创建成功。"),
        args_schema=CreateJourneyRequest,
    )
