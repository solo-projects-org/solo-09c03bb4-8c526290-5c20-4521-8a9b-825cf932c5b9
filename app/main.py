"""FastAPI 应用：遮盖值反推核查服务。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from . import solver
from .schemas import (
    ConflictResponse,
    EX_HALL_CONFLICT,
    EX_RANGE,
    EX_RECOVERABLE,
    EX_TOTAL_CONFLICT,
    FeasibleResponse,
    HealthResponse,
    SuppressionRequest,
)

app = FastAPI(
    title="医院计数矩阵遮盖核查 API",
    description=(
        "发布分年龄组、乡镇的病例计数前，检查 null 遮盖格能否被公开的"
        "行/列合计精确反推。对每个隐藏格返回可行最小值、最大值及分别"
        "达到两界的完整可行矩阵；两界相同标记为可还原；无解时返回冲突。"
    ),
    version="1.0.0",
)


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["运维"],
    summary="健康检查",
)
def health() -> dict:
    return {"status": "ok", "service": "suppression-check"}


@app.post(
    "/api/v1/suppression-check",
    response_model=FeasibleResponse,
    responses={409: {"model": ConflictResponse, "description": "约束无解"}},
    tags=["核查"],
    summary="提交计数矩阵与公开合计，核查隐藏格反推风险",
)
def suppression_check(
    payload: Annotated[
        SuppressionRequest,
        Body(
            openapi_examples={
                "feasible_range": {
                    "summary": "可行：隐藏格存在取值区间（不可还原）",
                    "description": "3x3 矩阵，6 个遮盖格只能确定到区间，工程师需调整遮盖位置",
                    "value": EX_RANGE,
                },
                "feasible_recoverable": {
                    "summary": "可行：隐藏格被合计唯一锁定（可还原）",
                    "description": "4 个遮盖格的 min == max，存在精确反推风险",
                    "value": EX_RECOVERABLE,
                },
                "conflict_totals": {
                    "summary": "冲突：行列总合计不相等",
                    "value": EX_TOTAL_CONFLICT,
                },
                "conflict_hall": {
                    "summary": "冲突：总量相等但结构无解（Hall 条件）",
                    "description": "最大流小于所需流量，返回最小割冲突见证",
                    "value": EX_HALL_CONFLICT,
                },
            }
        ),
    ]
):
    try:
        result = solver.analyze(
            matrix=payload.matrix,
            row_totals=payload.row_totals,
            col_totals=payload.col_totals,
        )
    except solver.SuppressionConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "status": "conflict",
                "reasons": exc.reasons,
                "witness": exc.witness,
            },
        )
    return result


@app.get(
    "/method",
    tags=["核查"],
    summary="算法说明",
)
def method() -> dict:
    return solver.fraction_explanation()
