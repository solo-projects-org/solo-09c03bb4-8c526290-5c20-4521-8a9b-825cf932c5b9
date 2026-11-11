"""FastAPI 应用：月报底表归并 + 遮盖值反推核查服务。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from . import merger, solver
from .schemas import (
    ConflictResponse,
    EX_HALL_CONFLICT,
    EX_RANGE,
    EX_RECOVERABLE,
    EX_TOTAL_CONFLICT,
    FeasibleResponse,
    HealthResponse,
    MergeRejectedResponse,
    SuppressionRequest,
)

app = FastAPI(
    title="医院数据底表归并与遮盖核查 API",
    description=(
        "一、把多个院区的月报底表按脱敏病例编号流式归并为不重号的 CSV；"
        "二、发布分年龄组、乡镇的病例计数前，检查 null 遮盖格能否被公开的"
        "行/列合计精确反推。归并接口在全部文件校验通过后才返回 CSV，"
        "任何逆序、格式错误或同号冲突都会退回整批并给出来源文件与行号。"
    ),
    version="1.1.0",
)


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["运维"],
    summary="健康检查",
)
def health() -> dict:
    return {"status": "ok", "service": "suppression-check"}


def _uploaded_files(form) -> list[UploadFile]:
    """收集 multipart 表单中的所有文件字段（兼容 files/file/uploads/upload）。"""
    return [
        value
        for _, value in form.multi_items()
        if isinstance(value, UploadFile)
    ]


@app.post(
    "/api/v1/merge",
    tags=["归并"],
    summary="归并多院区月报底表（最多 8 份 CSV）",
    description=(
        "一次接收最多 8 份 UTF-8 CSV，列名固定为 `病例编号,乡镇,年龄组`，"
        "每份文件内部按病例编号字典序升序排列。相同编号的记录只有在乡镇和"
        "年龄组都一致时才合并为一行。\n\n"
        "全部文件读完且校验通过后，返回按病例编号升序排列的 CSV（流式下载）；"
        "遇到逆序、格式错误或同号冲突则退回整批，返回 JSON 并指出"
        "来源文件与原始行号（表头为第 1 行）。同号冲突会列出两处来源。\n\n"
        "Swagger 上传入口：可在文件选择框中同时选中随附的 "
        "`samples/campus_a.csv` 与 `samples/campus_b.csv` 两个跨院区重号样例。"
    ),
    responses={
        200: {
            "description": "校验通过后的归并 CSV（UTF-8，按病例编号升序）",
            "content": {"text/csv": {"schema": {"type": "string"}}},
        },
        409: {
            "model": MergeRejectedResponse,
            "description": "同号记录的乡镇或年龄组冲突，整批退回",
        },
        422: {
            "model": MergeRejectedResponse,
            "description": "文件数超限、空批、逆序或 CSV/UTF-8 格式错误，整批退回",
        },
    },
    # 实际处理兼容任意文件字段名；Swagger 只展示标准字段 files。
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["files"],
                        "properties": {
                            "files": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                                "description": (
                                    "1~8 份 UTF-8 CSV；每份列为 "
                                    "病例编号,乡镇,年龄组。可一次选中 "
                                    "samples/campus_a.csv 与 samples/campus_b.csv"
                                ),
                            }
                        },
                    }
                }
            },
        }
    },
)
@app.post("/api/v1/merge-cases", include_in_schema=False)
@app.post("/api/v1/cases/merge", include_in_schema=False)
@app.post("/api/v1/merge-records", include_in_schema=False)
@app.post("/api/v1/case-merge", include_in_schema=False)
@app.post("/merge", include_in_schema=False)
async def merge_case_tables(request: Request):
    try:
        form = await request.form()
    except Exception as exc:  # malformed multipart / missing boundary
        return JSONResponse(
            status_code=422,
            content=merger.MergeError(
                "format_error",
                f"无法解析 multipart 上传：{exc}",
            ).to_payload(),
        )

    uploaded = _uploaded_files(form)
    sources = []
    for index, item in enumerate(uploaded, start=1):
        filename = item.filename or f"file[{index}]"
        sources.append((filename, index, item.file))

    try:
        output_path = await run_in_threadpool(merger.merge_uploads, sources)
    except merger.MergeError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())
    finally:
        # 归并完成后即可释放上传产生的临时文件（下载用的 output_path 不受影响）。
        try:
            await form.close()
        except Exception:
            pass

    return FileResponse(
        output_path,
        media_type="text/csv",
        filename="merged.csv",
        background=BackgroundTask(merger.safe_unlink, output_path),
    )



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
