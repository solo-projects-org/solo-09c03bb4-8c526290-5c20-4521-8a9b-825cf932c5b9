"""请求/响应 Pydantic 模型与 Swagger 示例。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator

# 示例数据（行列序号均从 0 开始，与请求中的矩阵下标一致）---------------

# 示例 1：3x3，4 个隐藏格构成自由块，取值为区间，不可还原。
EX_RANGE: dict = {
    "matrix": [
        [5, None, None],
        [None, 3, None],
        [None, None, 2],
    ],
    "row_totals": [12, 10, 8],
    "col_totals": [9, 9, 12],
}

# 示例 2：3x3，隐藏格被合计完全锁定，全部可还原。
EX_RECOVERABLE: dict = {
    "matrix": [
        [2, None, 3],
        [None, 4, None],
        [5, 1, None],
    ],
    "row_totals": [10, 10, 9],
    "col_totals": [9, 10, 10],
}

# 示例 3：行合计之和与列合计之和不相等，直接冲突。
EX_TOTAL_CONFLICT: dict = {
    "matrix": [
        [1, None],
        [None, 2],
    ],
    "row_totals": [5, 5],
    "col_totals": [6, 6],
}

# 示例 4：总量相等但运输结构无解（第 0 行只能把量送进第 0 列，容量不够）。
EX_HALL_CONFLICT: dict = {
    "matrix": [
        [None, 4],
        [3, None],
    ],
    "row_totals": [10, 7],
    "col_totals": [5, 12],
}


class SuppressionRequest(BaseModel):
    """核查请求：计数矩阵（null 表示遮盖格）与公开行/列合计。"""

    matrix: list[list[StrictInt | None]] = Field(
        ...,
        description=(
            "最多 12x12 的非负整数计数矩阵；null 表示被遮盖的格子，"
            "其余数值视为已公开的固定格"
        ),
    )
    row_totals: list[StrictInt] = Field(
        ..., description="每一行的公开合计，长度须等于矩阵行数"
    )
    col_totals: list[StrictInt] = Field(
        ..., description="每一列的公开合计，长度须等于矩阵列数"
    )

    @field_validator("matrix")
    @classmethod
    def _validate_matrix_cells(cls, value):
        # StrictInt 在联合类型里仍可能放行 bool（bool 是 int 子类），显式拒绝；
        # 同时统一拦截负数
        for i, row in enumerate(value):
            for j, cell in enumerate(row):
                if isinstance(cell, bool):
                    raise ValueError(
                        f"matrix[{i}][{j}] 不能是布尔值，须为整数或 null"
                    )
                if cell is not None and cell < 0:
                    raise ValueError(f"matrix[{i}][{j}] 不能为负数：{cell}")
        return value

    @field_validator("row_totals", "col_totals")
    @classmethod
    def _validate_totals(cls, value):
        for i, cell in enumerate(value):
            if isinstance(cell, bool):
                raise ValueError(f"合计值不能是布尔值：位置 {i}")
            if cell < 0:
                raise ValueError(f"合计值不能为负数：位置 {i} = {cell}")
        return value

    @model_validator(mode="after")
    def _check_shape(self) -> "SuppressionRequest":
        if not self.matrix:
            raise ValueError("matrix 至少要包含一行")
        if len(self.matrix) > 12:
            raise ValueError("matrix 行数不能超过 12")
        width = len(self.matrix[0])
        if width == 0 or width > 12:
            raise ValueError("matrix 列数须在 1..12 之间")
        for i, row in enumerate(self.matrix):
            if len(row) != width:
                raise ValueError(f"第 {i} 行长度与首行不一致（应为 {width}）")
        if len(self.row_totals) != len(self.matrix):
            raise ValueError("row_totals 长度须等于矩阵行数")
        if len(self.col_totals) != width:
            raise ValueError("col_totals 长度须等于矩阵列数")
        if any(v < 0 for v in self.row_totals):
            raise ValueError("row_totals 不能包含负数")
        if any(v < 0 for v in self.col_totals):
            raise ValueError("col_totals 不能包含负数")
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"summary": "可行：隐藏格存在取值区间（不可还原）", "value": EX_RANGE},
                {"summary": "可行：隐藏格被合计唯一锁定（可还原）", "value": EX_RECOVERABLE},
                {"summary": "冲突：行列总合计不相等", "value": EX_TOTAL_CONFLICT},
                {"summary": "冲突：总量相等但结构无解（Hall 条件）", "value": EX_HALL_CONFLICT},
            ]
        }
    }


class Dimensions(BaseModel):
    rows: int
    cols: int


class CellRange(BaseModel):
    row: int = Field(..., description="隐藏格行下标（从 0 开始）")
    col: int = Field(..., description="隐藏格列下标（从 0 开始）")
    min: int = Field(..., description="在全部合计同时成立前提下该格的最小取值")
    max: int = Field(..., description="在全部合计同时成立前提下该格的最大取值")
    recoverable: bool = Field(
        ..., description="min == max 时为 true，表示可被公开合计精确反推"
    )


class FeasibleResponse(BaseModel):
    status: Literal["feasible"]
    dimensions: Dimensions
    grand_total: int = Field(..., description="行合计之和（等于列合计之和）")
    hidden_count: int
    recoverable_count: int = Field(..., description="可还原隐藏格数量")
    cells: list[CellRange] = Field(
        ..., description="每个隐藏格的取值区间与可还原标记，按行优先顺序"
    )
    minimizer_matrix: list[list[int]] = Field(
        ..., description="达到各隐藏格下界的完整可行矩阵（对首个隐藏格取 min）"
    )
    maximizer_matrix: list[list[int]] = Field(
        ..., description="达到各隐藏格上界的完整可行矩阵（对首个隐藏格取 max）"
    )


class ConflictWitness(BaseModel):
    max_flow: int
    required_flow: int
    row_subset: list[int] = Field(
        ..., description="最小割源侧的行下标集合 R（0 起）"
    )
    neighbor_cols: list[int] = Field(
        ..., description="与 R 中隐藏格相邻的列下标集合 N(R)"
    )
    subset_demand: int = Field(..., description="行子集 R 的隐藏格总需求")
    neighbor_supply: int = Field(
        ..., description="邻接列 N(R) 能承接的总供给，小于需求即无解"
    )


class ConflictResponse(BaseModel):
    status: Literal["conflict"]
    reasons: list[str] = Field(
        ..., description="冲突的文字说明；无解时不返回任何取值区间"
    )
    witness: ConflictWitness | None = Field(
        default=None, description="最大流最小割给出的 Hall 型冲突见证（若适用）"
    )


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


# ---------------------------------------------------------------------------
# 归并接口的退回响应
# ---------------------------------------------------------------------------

class MergeSourceLocation(BaseModel):
    file: str = Field(..., description="上传时的原始文件名")
    upload_index: int = Field(..., description="本批次中的文件序号，从 1 开始")
    line: int = Field(..., description="原始物理行号，表头为第 1 行")
    case_id: str | None = None
    township: str | None = None
    age_group: str | None = None


class MergeErrorItem(BaseModel):
    type: str = Field(
        ...,
        description=(
            "format_error / out_of_order / duplicate_conflict / "
            "too_many_files / empty_batch"
        ),
    )
    message: str
    file: str | None = Field(default=None, description="出错来源文件")
    line: int | None = Field(default=None, description="出错记录起始物理行号")
    sources: list[MergeSourceLocation] | None = Field(
        default=None, description="同号冲突或逆序涉及的两处来源"
    )


class MergeRejectedResponse(BaseModel):
    status: Literal["rejected"]
    error_type: str
    message: str
    detail: str
    error: MergeErrorItem
    errors: list[MergeErrorItem]
    # 便于客户端直接读取的顶层定位字段（与 error 内字段一致）
    file: str | None = None
    filename: str | None = None
    source_file: str | None = None
    line: int | None = None
    line_number: int | None = None
    sources: list[MergeSourceLocation] | None = None
    max_files: int | None = None
    uploaded_files: list[str] | None = None
    encoding_error: str | None = None

