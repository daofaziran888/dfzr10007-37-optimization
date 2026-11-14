"""自助SQL查询接口：维护查询脚本，覆盖分享脚本、归档脚本、删除脚本等动作。

结果缓存按脚本编号与参数指纹建立多版本：执行历史列出每版来源，
批量执行整组一次提交、逐条返回缓存版本，失败脚本只重试自身。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    ActionResult,
    EntryPayload,
    PageResult,
    SqlBatchPayload,
    SqlBatchResult,
    SqlExecuteItem,
    SqlExecuteResult,
)
from app.services.sqlpad import SqlpadService

router = APIRouter(prefix="/api/sqlpad", tags=["自助SQL查询"])

service = SqlpadService()

LIST_FIELDS = ["脚本编号", "脚本名称", "SQL语句", "参数定义", "执行历史", "结果缓存", "创建人", "脚本状态"]
STATUSES = ["草稿", "已保存", "已分享", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按脚本编号检索"),
    status: str | None = Query(default=None, description="草稿、已保存、已分享、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按脚本编号与状态过滤自助SQL查询列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出自助SQL查询清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "sqlpad", "total": total, "items": items}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条查询脚本，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="查询脚本已登记", entry=entry)


@router.post("/execute", response_model=SqlExecuteResult)
def execute_one(payload: SqlExecuteItem) -> SqlExecuteResult:
    """单脚本执行：同一参数指纹复用唯一快照；失败只影响本脚本，可单独重试。"""
    return SqlExecuteResult(**service.execute_one(payload.脚本编号, payload.参数))


@router.post("/execute/batch", response_model=SqlBatchResult)
def execute_batch(payload: SqlBatchPayload) -> SqlBatchResult:
    """批量执行：多选脚本整组一次提交，按脚本与参数指纹归并缓存，逐条返回缓存版本。"""
    outcome = service.execute_batch([item.model_dump() for item in payload.items])
    return SqlBatchResult(**outcome)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条查询脚本明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询脚本 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/cache")
def list_cache_versions(entry_id: int) -> dict[str, Any]:
    """结果缓存多版本清单：按脚本编号+参数指纹归档，旧参数版本仍可回看。"""
    entry, versions = service.list_cache_versions(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询脚本 {entry_id} 不存在或已归档")
    return {"脚本编号": entry.get("脚本编号"), "total": len(versions), "items": versions}


@router.get("/{entry_id}/history")
def list_execution_history(entry_id: int) -> dict[str, Any]:
    """执行历史：每个执行版本都标注来源（新建快照、复用快照、同组归并）。"""
    entry, items = service.list_history(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询脚本 {entry_id} 不存在或已归档")
    return {"脚本编号": entry.get("脚本编号"), "total": len(items), "items": items}


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条查询脚本执行分享脚本、归档脚本、删除脚本；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
