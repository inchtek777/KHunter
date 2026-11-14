"""
行业数据 API

提供：
  - 行业板块列表（东方财富优先，接口不可用时自动降级同花顺）
  - 关注行业增删查
  - 关注行业日 K（行业指数）抓取/查询/清理
"""

import logging
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request

from utils.industry_board_service import (
    IndustryBoardError,
    IndustryBoardService,
    get_industry_service,
)
from trading.industry_rotation import (
    IndustryRotationService,
    RotationError,
    get_rotation_task,
    start_rotation_task,
)

logger = logging.getLogger(__name__)

industry_bp = Blueprint("industry", __name__, url_prefix="/api/industry")


def _ok(data: Any = None, **extra):
    payload: Dict[str, Any] = {"success": True, "data": data}
    payload.update(extra)
    return jsonify(payload)


def _fail(message: str, status: int = 400, **extra):
    payload: Dict[str, Any] = {"success": False, "error": message}
    payload.update(extra)
    return jsonify(payload), status


def _service() -> IndustryBoardService:
    return get_industry_service()


def _json_body() -> Dict[str, Any]:
    return request.get_json(silent=True) or {}


def _rows_source(rows: List[Dict[str, Any]]) -> str:
    sources = {row.get("source") for row in rows if row.get("source")}
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed" if sources else ""


@industry_bp.route("/summary", methods=["GET"])
def industry_summary():
    """行业数据汇总（关注数、日K条数、最新日期）"""
    try:
        return _ok(_service().get_summary())
    except Exception as exc:
        logger.exception("获取行业数据汇总失败")
        return _fail(f"获取行业数据汇总失败: {exc}", 500)


@industry_bp.route("/list", methods=["GET"])
def industry_list():
    """行业板块列表（东方财富优先，失败自动降级同花顺）

    参数：
        keyword: 代码/名称模糊搜索（可选）
        refresh: 1 表示先从东方财富刷新再返回（东财不可用时自动降级同花顺）
    """
    keyword = request.args.get("keyword", "")
    refresh = str(request.args.get("refresh", "0")).lower() in ("1", "true", "yes", "on")
    service = _service()

    if refresh:
        try:
            result = service.refresh_industry_list()
            rows = service.get_industry_list(keyword=keyword)
            warning = None
            if result.get("fallback"):
                warning = "东方财富接口不可用，已自动切换同花顺行业数据"
            return _ok(
                rows,
                count=len(rows),
                refreshed=result["count"],
                source=result["source"],
                fallback=bool(result.get("fallback")),
                warning=warning,
            )
        except IndustryBoardError as exc:
            # 刷新失败时退回本地缓存，保证页面仍可用
            rows = service.get_industry_list(keyword=keyword, auto_refresh=False)
            if rows:
                return _ok(
                    rows,
                    count=len(rows),
                    stale=True,
                    warning=str(exc),
                    source=_rows_source(rows),
                )
            return _fail(str(exc), 502)

    try:
        rows = service.get_industry_list(keyword=keyword)
        return _ok(rows, count=len(rows), source=_rows_source(rows))
    except IndustryBoardError as exc:
        rows = service.get_industry_list(keyword=keyword, auto_refresh=False)
        if rows:
            return _ok(rows, count=len(rows), stale=True, warning=str(exc))
        return _fail(str(exc), 502)
    except Exception as exc:
        logger.exception("获取行业列表失败")
        return _fail(f"获取行业列表失败: {exc}", 500)


@industry_bp.route("/watched", methods=["GET"])
def industry_watched():
    """已关注行业列表（附日K统计）"""
    try:
        return _ok(_service().get_watched())
    except Exception as exc:
        logger.exception("获取关注行业失败")
        return _fail(f"获取关注行业失败: {exc}", 500)


@industry_bp.route("/watch", methods=["POST"])
def industry_watch_add():
    """添加关注行业

    请求体：
        {"industry_code": "BK1027", "industry_name": "小金属"}
    """
    body = _json_body()
    code = body.get("industry_code") or body.get("code")
    name = body.get("industry_name") or body.get("name")
    if not code:
        return _fail("industry_code 不能为空")
    try:
        row = _service().add_watch(code, name)
        return _ok(row)
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:
        logger.exception("添加关注行业失败")
        return _fail(f"添加关注行业失败: {exc}", 500)


@industry_bp.route("/watch/<code>", methods=["DELETE"])
def industry_watch_remove(code: str):
    """取消关注行业

    参数：
        delete_data: 1 表示同时删除已保存的日K（默认保留）
    """
    delete_data = str(request.args.get("delete_data", "0")).lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    try:
        removed = _service().remove_watch(code, delete_data=delete_data)
        if not removed:
            return _fail(f"未关注行业: {code}", 404)
        return _ok({"industry_code": code, "delete_data": delete_data})
    except Exception as exc:
        logger.exception("取消关注行业失败")
        return _fail(f"取消关注行业失败: {exc}", 500)


@industry_bp.route("/daily/fetch", methods=["POST"])
def industry_daily_fetch():
    """抓取并保存关注行业日 K

    请求体：
        {
          "codes": ["BK1027"],   # 可选；不传且 all=true 时抓取全部关注行业
          "all": true,
          "start_date": "2025-01-01",
          "end_date": "2025-12-31"
        }
    """
    body = _json_body()
    service = _service()
    codes: List[str] = body.get("codes") or []
    if isinstance(codes, str):
        codes = [codes]
    if body.get("all") or not codes:
        codes = [row["industry_code"] for row in service.get_watched()]
    if not codes:
        return _fail("没有需要抓取的行业，请先添加关注行业")

    try:
        result = service.fetch_daily_many(
            codes,
            start_date=body.get("start_date"),
            end_date=body.get("end_date"),
        )
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:
        logger.exception("批量抓取行业日K失败")
        return _fail(f"批量抓取行业日K失败: {exc}", 500)

    if result["results"]:
        return _ok(result, saved=result["saved"], errors=result["errors"])
    return _fail(
        "全部行业日K抓取失败",
        502,
        data=result,
        errors=result["errors"],
    )


@industry_bp.route("/daily/<code>", methods=["GET"])
def industry_daily_query(code: str):
    """查询已保存的行业日 K（升序）

    参数：
        limit: 返回最近 N 条，默认 500
        start_date / end_date: 可选日期过滤
    """
    try:
        rows = _service().get_daily(
            code,
            limit=request.args.get("limit", 500),
            start_date=request.args.get("start_date"),
            end_date=request.args.get("end_date"),
        )
        return _ok(rows, count=len(rows), industry_code=code.upper())
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:
        logger.exception("查询行业日K失败")
        return _fail(f"查询行业日K失败: {exc}", 500)


@industry_bp.route("/daily/<code>", methods=["DELETE"])
def industry_daily_clear(code: str):
    """清空某个行业已保存的日 K"""
    try:
        deleted = _service().clear_daily(code)
        return _ok({"industry_code": code.upper(), "deleted": deleted})
    except Exception as exc:
        logger.exception("清空行业日K失败")
        return _fail(f"清空行业日K失败: {exc}", 500)

# ==================== 行业轮动回测 ====================


@industry_bp.route("/rotation/config", methods=["GET"])
def rotation_config():
    """轮动回测初始化：关注行业、行业指数覆盖、默认参数"""
    try:
        return _ok(IndustryRotationService().get_config())
    except Exception as exc:
        logger.exception("获取轮动配置失败")
        return _fail(f"获取轮动配置失败: {exc}", 500)


@industry_bp.route("/rotation/start", methods=["POST"])
def rotation_start():
    """启动行业轮动回测（后台任务）

    请求体：
        {
          "candidate_limit": 30,     # 每个行业候选股数量（5~100）
          "optimize": true,          # 是否网格搜索参数
          "start_date": "2016-11-14",# 可选，默认最近 10 年
          "end_date": "2026-11-13"   # 可选，默认行业指数最新日期
        }
    """
    body = _json_body()
    params = {
        "candidate_limit": body.get("candidate_limit", 20),
        "optimize": body.get("optimize", True),
        "objective": body.get("objective", "max_return"),
        "rebalance_days": body.get("rebalance_days", 5),
        "top_stocks": body.get("top_stocks", 2),
        "start_date": body.get("start_date"),
        "end_date": body.get("end_date"),
    }
    try:
        task_id = start_rotation_task(params)
        return _ok({"task_id": task_id}, task_id=task_id)
    except RotationError as exc:
        return _fail(str(exc), 409)
    except Exception as exc:
        logger.exception("启动轮动回测失败")
        return _fail(f"启动轮动回测失败: {exc}", 500)


@industry_bp.route("/rotation/status/<task_id>", methods=["GET"])
def rotation_status(task_id: str):
    """轮动回测进度"""
    task = get_rotation_task(task_id)
    if not task:
        return _fail("任务不存在或已过期", 404)
    return _ok(
        {
            "task_id": task_id,
            "status": task.get("status"),
            "progress": task.get("progress", 0),
            "phase": task.get("phase", ""),
            "message": task.get("message", ""),
            "error": task.get("error"),
            "result_ready": task.get("result") is not None,
        }
    )


@industry_bp.route("/rotation/result/<task_id>", methods=["GET"])
def rotation_result(task_id: str):
    """轮动回测结果"""
    task = get_rotation_task(task_id)
    if not task:
        return _fail("任务不存在或已过期", 404)
    if task.get("status") != "completed" or task.get("result") is None:
        return _fail(
            task.get("error") or "任务尚未完成",
            202,
            data={
                "task_id": task_id,
                "status": task.get("status"),
                "progress": task.get("progress", 0),
            },
        )
    return _ok(task["result"])

