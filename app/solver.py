"""区间反推核查的核心算法。

问题建模
--------
给定 r x c 非负整数计数矩阵，部分格子被遮盖（null），同时公开：

* 每一行的行合计 row_totals；
* 每一列的列合计 col_totals。

设第 k 个隐藏格的取值为变量 x_k（非负整数）。所有隐藏格与固定格必须
同时满足全部行/列合计等式，这是一个整数运输可行性问题。

运输问题的约束矩阵是全幺模矩阵（totally unimodular），且右侧为整数，
因此线性规划松弛的最优顶点天然是整数解。本模块用 scipy 的 HiGHS LP
求解器分别对每个隐藏格做 min / max 两次求解：

* 最小值 == 最大值 -> 该格可被公开合计唯一反推（可还原）；
* 否则只暴露该格的取值闭区间。

为在无解时给出结构化的冲突说明，另实现一个 Dinic 最大流：隐藏格作为
行节点到列节点之间容量无限的运输边，最大流达不到总计数即无解，并可
由最小割构造 Hall 型冲突见证（某些乡镇的需求量大于对应年龄组的供给量）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_matrix


class SuppressionConflict(ValueError):
    """约束无解时抛出，携带面向用户的冲突说明。"""

    def __init__(self, reasons: list[str], witness: dict | None = None):
        super().__init__("; ".join(reasons) if reasons else "infeasible constraints")
        self.reasons = reasons
        self.witness = witness


@dataclass(frozen=True)
class HiddenCell:
    index: int  # 变量序号
    row: int
    col: int


# ---------------------------------------------------------------------------
# 输入检查
# ---------------------------------------------------------------------------

def validate_request(
    matrix: list[list[int | None]],
    row_totals: list[int],
    col_totals: list[int],
) -> tuple[int, int]:
    """防御性尺寸校验。

    API 入口的 Pydantic 模型（schemas.SuppressionRequest）已完成严格的
    形状、类型与非负校验并返回 422；这里保留同构检查，保证 solver 可被
    独立调用。
    """
    if not matrix or not matrix[0]:
        raise SuppressionConflict(["matrix 不能为空矩阵"])
    rows = len(matrix)
    cols = len(matrix[0])
    if rows > 12 or cols > 12:
        raise SuppressionConflict(["matrix 尺寸不能超过 12x12"])
    if any(len(row) != cols for row in matrix):
        raise SuppressionConflict(["matrix 各行长度不一致"])
    if len(row_totals) != rows or len(col_totals) != cols:
        raise SuppressionConflict(["row_totals/col_totals 长度与矩阵尺寸不一致"])
    if any(
        (v is not None) and (not isinstance(v, int) or isinstance(v, bool) or v < 0)
        for row in matrix
        for v in row
    ) or any(v < 0 for v in row_totals + col_totals):
        raise SuppressionConflict(["所有计数与合计必须为非负整数"])
    return rows, cols


# ---------------------------------------------------------------------------
# Dinic 最大流：可行性判定与冲突见证
# ---------------------------------------------------------------------------

class _Edge:
    __slots__ = ("to", "rev", "cap")

    def __init__(self, to: int, rev: int, cap: int):
        self.to = to
        self.rev = rev
        self.cap = cap


class _Dinic:
    def __init__(self, n: int):
        self.g: list[list[_Edge]] = [[] for _ in range(n)]
        self.level = [-1] * n
        self.it = [0] * n

    def add_edge(self, fr: int, to: int, cap: int) -> None:
        forward = _Edge(to, len(self.g[to]), cap)
        backward = _Edge(fr, len(self.g[fr]), 0)
        self.g[fr].append(forward)
        self.g[to].append(backward)

    def _bfs(self, s: int, t: int) -> bool:
        self.level = [-1] * len(self.g)
        self.level[s] = 0
        queue = [s]
        head = 0
        while head < len(queue):
            v = queue[head]
            head += 1
            for e in self.g[v]:
                if e.cap > 0 and self.level[e.to] < 0:
                    self.level[e.to] = self.level[v] + 1
                    queue.append(e.to)
        return self.level[t] >= 0

    def _dfs(self, v: int, t: int, f: int) -> int:
        if v == t:
            return f
        while self.it[v] < len(self.g[v]):
            e = self.g[v][self.it[v]]
            if e.cap > 0 and self.level[e.to] == self.level[v] + 1:
                d = self._dfs(e.to, t, min(f, e.cap))
                if d > 0:
                    e.cap -= d
                    self.g[e.to][e.rev].cap += d
                    return d
            self.it[v] += 1
        return 0

    def max_flow(self, s: int, t: int, limit: int) -> int:
        flow = 0
        while flow < limit and self._bfs(s, t):
            self.it = [0] * len(self.g)
            while flow < limit:
                f = self._dfs(s, t, limit - flow)
                if f == 0:
                    break
                flow += f
        return flow

    def reachable_from(self, s: int) -> list[bool]:
        seen = [False] * len(self.g)
        seen[s] = True
        stack = [s]
        while stack:
            v = stack.pop()
            for e in self.g[v]:
                if e.cap > 0 and not seen[e.to]:
                    seen[e.to] = True
                    stack.append(e.to)
        return seen


def _maxflow_feasibility(
    rows: int,
    cols: int,
    hidden: list[HiddenCell],
    row_demand: list[int],
    col_supply: list[int],
    total: int,
) -> dict | None:
    """返回 None 表示可行；否则返回冲突见证字典。

    网络：source -> 行节点（容量=该行列合计扣除固定格后的剩余需求），
    行节点 -> 列节点（仅隐藏格之间有边，容量无限），列节点 -> sink
    （容量=该列剩余供给）。
    """
    source = 0
    row_base = 1
    col_base = row_base + rows
    sink = col_base + cols
    dinic = _Dinic(sink + 1)
    inf = total + 1

    for i, d in enumerate(row_demand):
        if d > 0:
            dinic.add_edge(source, row_base + i, d)
    for j, s in enumerate(col_supply):
        if s > 0:
            dinic.add_edge(col_base + j, sink, s)
    for h in hidden:
        dinic.add_edge(row_base + h.row, col_base + h.col, inf)

    flow = dinic.max_flow(source, sink, total)
    if flow == total:
        return None

    reachable = dinic.reachable_from(source)
    r_subset = [
        i for i in range(rows) if reachable[row_base + i]
    ]
    # 与 R 之间存在隐藏格运输边的列即邻接列 N(R)；最小割保证 N(R) 全部
    # 可达（若不可达则该运输边横跨割集、容量为 inf，与有限最小割矛盾）。
    neighbor_cols = sorted({h.col for h in hidden if h.row in set(r_subset)})
    demand = sum(row_demand[i] for i in r_subset)
    neighbor_supply = sum(col_supply[j] for j in neighbor_cols)
    return {
        "max_flow": flow,
        "required_flow": total,
        "row_subset": r_subset,
        "neighbor_cols": neighbor_cols,
        "subset_demand": demand,
        "neighbor_supply": neighbor_supply,
    }


# ---------------------------------------------------------------------------
# LP 求解
# ---------------------------------------------------------------------------

def _build_constraints(
    rows: int,
    cols: int,
    hidden: list[HiddenCell],
    row_demand: list[int],
    col_supply: list[int],
):
    """构造 A_eq x = b_eq：行等式与列等式（可能含冗余行，HiGHS 可处理）。"""
    n = len(hidden)
    data: list[float] = []
    row_idx: list[int] = []
    col_idx: list[int] = []
    b: list[int] = []

    eq_row = 0
    for h in hidden:
        row_idx.append(h.row)
        col_idx.append(h.index)
        data.append(1.0)
    b.extend(row_demand)
    eq_row += rows

    for h in hidden:
        row_idx.append(eq_row + h.col)
        col_idx.append(h.index)
        data.append(1.0)
    b.extend(col_supply)

    a_eq = coo_matrix((data, (row_idx, col_idx)), shape=(rows + cols, n)).tocsr()
    return a_eq, np.array(b, dtype=float)


def _round_integer(value: float) -> int:
    r = int(round(value))
    if abs(value - r) > max(1e-6, 1e-9 * abs(value)):
        raise SuppressionConflict(
            [f"求解器返回非整数解 {value}，请检查输入规模或联系维护者"]
        )
    return r


def _solve_lp(c: np.ndarray, a_eq, b_eq: np.ndarray) -> np.ndarray:
    result = linprog(
        c=c,
        A_eq=a_eq,
        b_eq=b_eq,
        bounds=(0, None),
        method="highs",
    )
    if not result.success:
        # 正常情况下最大流已先行判定可行，走到这里属于求解器层面的异常。
        raise SuppressionConflict(
            [f"线性规划求解失败（status={result.status}）：{result.message}"]
        )
    return result.x


def _fill_matrix(
    base: list[list[int | None]],
    hidden: list[HiddenCell],
    values: np.ndarray,
) -> list[list[int]]:
    matrix = [row[:] for row in base]
    for h in hidden:
        matrix[h.row][h.col] = _round_integer(float(values[h.index]))
    return matrix


def _verify_solution(
    matrix: list[list[int]],
    row_totals: list[int],
    col_totals: list[int],
) -> None:
    rows = len(matrix)
    cols = len(matrix[0])
    for i in range(rows):
        if sum(matrix[i]) != row_totals[i]:
            raise SuppressionConflict(
                [f"内部校验失败：第 {i} 行合计与公开值不一致"]
            )
    for j in range(cols):
        if sum(matrix[i][j] for i in range(rows)) != col_totals[j]:
            raise SuppressionConflict(
                [f"内部校验失败：第 {j} 列合计与公开值不一致"]
            )
    if any(v < 0 for row in matrix for v in row):
        raise SuppressionConflict(["内部校验失败：解中出现负数"])


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def analyze(
    matrix: list[list[int | None]],
    row_totals: list[int],
    col_totals: list[int],
) -> dict:
    """执行完整核查，返回可直接序列化为 API 响应的字典。

    无解时抛出 :class:`SuppressionConflict`。
    """
    rows, cols = validate_request(matrix, row_totals, col_totals)

    row_sum = sum(row_totals)
    col_sum = sum(col_totals)
    if row_sum != col_sum:
        raise SuppressionConflict([
            f"行合计之和（{row_sum}）与列合计之和（{col_sum}）不相等，"
            "任何矩阵都无法同时满足两组合计"
        ])
    total = row_sum

    fixed_row = [0] * rows
    fixed_col = [0] * cols
    hidden: list[HiddenCell] = []
    for i, row in enumerate(matrix):
        for j, v in enumerate(row):
            if v is None:
                hidden.append(HiddenCell(index=len(hidden), row=i, col=j))
            else:
                fixed_row[i] += v
                fixed_col[j] += v

    row_demand = [row_totals[i] - fixed_row[i] for i in range(rows)]
    col_supply = [col_totals[j] - fixed_col[j] for j in range(cols)]

    reasons: list[str] = []
    for i, d in enumerate(row_demand):
        if d < 0:
            reasons.append(
                f"第 {i} 行固定格之和（{fixed_row[i]}）已超过公开行合计"
                f"（{row_totals[i]}）"
            )
    for j, s in enumerate(col_supply):
        if s < 0:
            reasons.append(
                f"第 {j} 列固定格之和（{fixed_col[j]}）已超过公开列合计"
                f"（{col_totals[j]}）"
            )
    if reasons:
        raise SuppressionConflict(reasons)

    # 固定格之外、必须由隐藏格承担的总流量（行、列两侧应相等）。
    hidden_total = sum(row_demand)
    if hidden_total != sum(col_supply) or any(d < 0 for d in row_demand + col_supply):
        # 负值已在上面收集；此处为防御性总量检查。
        raise SuppressionConflict(reasons or ["隐藏格需要满足的总流量不一致"])

    # 没有隐藏格：直接核对固定矩阵是否等于公开合计。
    if not hidden:
        fixed_matrix: list[list[int]] = [
            [v for v in row] for row in matrix  # type: ignore[arg-type]
        ]
        _verify_solution(fixed_matrix, row_totals, col_totals)
        return {
            "status": "feasible",
            "dimensions": {"rows": rows, "cols": cols},
            "grand_total": total,
            "hidden_count": 0,
            "recoverable_count": 0,
            "cells": [],
        }

    witness = _maxflow_feasibility(
        rows, cols, hidden, row_demand, col_supply, hidden_total
    )
    if witness is not None:
        r_set = ", ".join(str(i) for i in witness["row_subset"]) or "（空集）"
        c_set = (
            ", ".join(str(j) for j in witness["neighbor_cols"]) or "（无邻接列）"
        )
        reasons = [
            "不存在满足全部公开合计的非负整数矩阵：最大流可行性检验未通过",
            f"行子集 [{r_set}] 的隐藏格需求合计为 {witness['subset_demand']}，"
            f"但这些行的隐藏格只能进入列 [{c_set}]，其可承接供给仅为 "
            f"{witness['neighbor_supply']}（Hall 条件被破坏：需求 > 邻接供给）",
            f"最大流 {witness['max_flow']} < 隐藏格所需总流量 "
            f"{witness['required_flow']}",
        ]
        raise SuppressionConflict(reasons, witness=witness)

    a_eq, b_eq = _build_constraints(rows, cols, hidden, row_demand, col_supply)
    n = len(hidden)

    cell_results: list[dict] = []
    recoverable_count = 0
    # 先求一版零目标解，作为未单独触发极值时的兜底可行矩阵。
    fallback = _solve_lp(np.zeros(n), a_eq, b_eq)
    min_matrix = max_matrix = None

    for h in hidden:
        c = np.zeros(n)
        c[h.index] = 1.0

        x_min = _solve_lp(c.copy(), a_eq, b_eq)
        min_value = _round_integer(float(x_min[h.index]))

        x_max = _solve_lp(-c, a_eq, b_eq)
        max_value = _round_integer(float(x_max[h.index]))

        if min_value > max_value:  # 防御性检查，正常不会发生
            raise SuppressionConflict([
                f"隐藏格 ({h.row}, {h.col}) 求得最小值 {min_value} 大于最大值 "
                f"{max_value}，求解异常"
            ])

        recoverable = min_value == max_value
        if recoverable:
            recoverable_count += 1

        cell_results.append(
            {
                "row": h.row,
                "col": h.col,
                "min": min_value,
                "max": max_value,
                "recoverable": recoverable,
            }
        )

        # 用第一个变量的极值解固化完整矩阵，保证矩阵真实可达两界。
        if min_matrix is None:
            min_matrix = _fill_matrix(matrix, hidden, x_min)
            max_matrix = _fill_matrix(matrix, hidden, x_max)

    if min_matrix is None or max_matrix is None:
        filled = _fill_matrix(matrix, hidden, fallback)
        min_matrix = max_matrix = filled

    _verify_solution(min_matrix, row_totals, col_totals)
    _verify_solution(max_matrix, row_totals, col_totals)

    return {
        "status": "feasible",
        "dimensions": {"rows": rows, "cols": cols},
        "grand_total": total,
        "hidden_count": n,
        "recoverable_count": recoverable_count,
        "cells": cell_results,
        "minimizer_matrix": min_matrix,
        "maximizer_matrix": max_matrix,
    }


def fraction_explanation() -> dict:
    """返回算法说明（供 /method 端点展示）。"""
    return {
        "model": "整数运输可行性 / 线性规划（全幺模约束矩阵保证整数顶点）",
        "variables": "每个 null 格子对应一个非负整数变量",
        "constraints": "每个变量参与所在行、所在列的合计等式",
        "solver": "scipy.optimize.linprog(method='highs')，逐变量 min/max 各一次",
        "feasibility": "Dinic 最大流预判无解并给出 Hall 型冲突见证",
        "recoverable_rule": "min == max 时该隐藏格取值唯一，可被公开合计反推",
        "integrality_note": (
            "问题右侧为整数且运输约束矩阵全幺模，浮点 LP 的最优顶点即为"
            "整数解；输出前另做整数舍入与合计复核"
        ),
    }
