"""Query tools plus framework-HITL-gated single-Journey mutations."""

from pathlib import Path

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from jobagent.journey.management import (
    JourneyTarget,
    JourneyUpdate,
    change_journey,
    get_journey,
    list_journeys,
)


class JourneyListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(default="", max_length=200)
    include_deleted: bool = False
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class JourneyGetInput(BaseModel):
    journey_id: str = Field(min_length=1, max_length=100)


def build_journey_management_tools(path: Path):
    async def list_opportunity_journeys(**kwargs):
        return {"status": "completed", "journeys": list_journeys(path, **kwargs)}

    async def get_opportunity_journey(journey_id: str):
        try:
            return {"status": "completed", "journey": get_journey(path, journey_id)}
        except KeyError:
            return {"status": "not_found", "journey_id": journey_id}

    tools = [StructuredTool.from_function(
        coroutine=list_opportunity_journeys, name="list_opportunity_journeys",
        description="查询网页 Journey 列表（不是岗位进度登记册），包含 ID、版本、JD 和任务/产物数量。支持公司过滤、分页和包含已删除记录。",
        args_schema=JourneyListInput,
    ), StructuredTool.from_function(
        coroutine=get_opportunity_journey, name="get_opportunity_journey",
        description="按 ID 查询 Journey 详情，包括删除状态、版本、JD 历史、任务和产物；修改、删除或恢复前必须先查看。",
        args_schema=JourneyGetInput,
    )]
    for action, label in [("update", "修改"), ("delete", "软删除"), ("restore", "恢复")]:
        def build(action=action, label=label):
            schema = JourneyUpdate if action == "update" else JourneyTarget

            async def mutate(**kwargs):
                try:
                    result = change_journey(path, action, schema.model_validate(kwargs))
                    return {"status": "completed", "action": action, "journey": result}
                except KeyError:
                    return {"status": "not_found", "journey_id": kwargs.get("journey_id")}
                except ValueError as error:
                    return {"status": "conflict", "message": str(error), "next_action": "重新查询详情并请用户审批；不得自动重试修改。"}

            return StructuredTool.from_function(
                coroutine=mutate, name=f"{action}_opportunity_journey", args_schema=schema,
                description=f"{label}一个指定 ID 的 Journey，执行前暂停等待用户批准。必须传详情中读到的版本、公司、岗位和操作理由。软删除只隐藏列表，保留 JD、任务、产物，支持恢复；不允许仅按名称批量删除。",
            )
        tools.append(build())
    return tools
