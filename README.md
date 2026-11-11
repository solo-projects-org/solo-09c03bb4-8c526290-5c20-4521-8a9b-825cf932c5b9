# 医院数据底表归并与遮盖核查 API

本服务提供两个相互独立的能力：

1. **多院区月报底表归并**：把最多 8 份 UTF-8 CSV 按脱敏病例编号流式合成为
   一份不重号底表，供后续按乡镇、年龄组汇总。
2. **计数矩阵遮盖核查**：发布分年龄组（列）、乡镇（行）的病例计数前，检查
   `null` 遮盖格能否被公开的行/列合计精确反推。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 应用与路由（Swagger 示例/说明）
│   ├── merger.py      # 流式 k 路归并、逐行校验、磁盘临时结果
│   ├── schemas.py     # Pydantic 请求/响应模型与内置示例
│   └── solver.py      # 遮盖核查：Dinic 最大流 + HiGHS 线性规划
├── samples/
│   ├── campus_a.csv   # 院区 A 月报底表（与其他院区存在重号）
│   └── campus_b.csv   # 院区 B 月报底表（与 A 的重号记录字段一致）
├── tests/             # pytest 测试
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```

## 启动方式（Docker Compose）

```bash
docker compose up --build
```

启动后：

- Swagger UI：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>

停止：`docker compose down`。

### 在 Swagger 上传随附样例

打开 Swagger 的 `POST /api/v1/merge`，在 `files` 文件选择框中**同时选中**
仓库中的两份跨院区重号样例：

- `samples/campus_a.csv`
- `samples/campus_b.csv`

点击 Execute 后返回一份去重、按病例编号升序排列的 CSV。两份样例中
`C20260001`、`C20260005`、`C20260010` 是跨院区重号且乡镇/年龄组一致，
归并后各只保留一行；其余编号分别来自两个院区。

## 归并接口

`POST /api/v1/merge`（兼容别名 `/api/v1/merge-cases`、`/api/v1/cases/merge`、
`/api/v1/merge-records`、`/api/v1/case-merge`、`/merge`）

请求为 `multipart/form-data`，字段名 `files`，一次 1~8 份 UTF-8 CSV：

- 表头必须为 `病例编号,乡镇,年龄组`（允许 UTF-8 BOM 与表头字段两侧空白）；
- 每份文件内部按脱敏病例编号的字典序**非降序**排列；
- 每条记录恰好 3 列，字段不能为空；
- CSV 支持标准引号转义（字段内可含逗号、换行），也支持 CRLF/LF。

归并规则：

- 相同病例编号无论来自哪份文件，只要乡镇与年龄组都一致，就合并为一行；
- 同一编号的乡镇或年龄组不一致即为**同号冲突**，整批退回，并列出两处来源；
- 任一文件出现逆序、格式错误（表头/列数/空字段/非法 UTF-8）也会整批退回，
  响应指出**来源文件**与**原始物理行号**（表头为第 1 行）；
- 全部文件读完且全部校验通过后，才返回 `text/csv; charset=utf-8`，按病例
  编号升序排列，下载文件名为 `merged.csv`；出错时不会发送半份表。

### 流式处理

- 上传：逐块读取 `UploadFile`，不调用整文件读取；
- 归并：k 路归并（k ≤ 8），内存中只保留每份文件的当前记录与当前物理行；
- 结果：先写入磁盘临时文件，校验全部通过后才开始下载；任何错误都会删除
  临时文件并返回 JSON；
- 下载：`FileResponse` 分块读取临时文件，响应结束后自动删除。

### 退回响应

格式错误、空批、文件数超过 8 份或逆序返回 **422**；同号冲突返回 **409**。
响应同时提供结构化的 `error(s)` 和顶层定位字段：

```json
{
  "status": "rejected",
  "error_type": "duplicate_conflict",
  "message": "病例编号 \"C20260001\" 在文件 \"campus_a.csv\" 第 2 行与文件 \"campus_b.csv\" 第 2 行的乡镇或年龄组不一致",
  "error": {
    "type": "duplicate_conflict",
    "message": "病例编号 \"C20260001\" 在文件 \"campus_a.csv\" 第 2 行与文件 \"campus_b.csv\" 第 2 行的乡镇或年龄组不一致",
    "file": "campus_b.csv",
    "line": 2,
    "sources": [
      {"file": "campus_a.csv", "upload_index": 1, "line": 2, "case_id": "C20260001", "township": "东城镇", "age_group": "0-14岁"},
      {"file": "campus_b.csv", "upload_index": 2, "line": 2, "case_id": "C20260001", "township": "东城镇", "age_group": "15-64岁"}
    ]
  },
  "errors": [
    {
      "type": "duplicate_conflict",
      "message": "病例编号 \"C20260001\" 在文件 \"campus_a.csv\" 第 2 行与文件 \"campus_b.csv\" 第 2 行的乡镇或年龄组不一致",
      "file": "campus_b.csv",
      "line": 2,
      "sources": [
        {"file": "campus_a.csv", "upload_index": 1, "line": 2},
        {"file": "campus_b.csv", "upload_index": 2, "line": 2}
      ]
    }
  ],
  "file": "campus_b.csv",
  "line": 2,
  "sources": [
    {"file": "campus_a.csv", "upload_index": 1, "line": 2, "case_id": "C20260001", "township": "东城镇", "age_group": "0-14岁"},
    {"file": "campus_b.csv", "upload_index": 2, "line": 2, "case_id": "C20260001", "township": "东城镇", "age_group": "15-64岁"}
  ]
}
```

`error_type` 取值：`empty_batch`、`too_many_files`、`format_error`、
`out_of_order`、`duplicate_conflict`。

## 遮盖核查接口

`POST /api/v1/suppression-check`

对每个 `null` 隐藏格，在所有公开行/列合计同时成立的前提下返回可行的
`min` / `max`、分别达到两界的完整矩阵，以及 `min == max` 时的
`recoverable` 标记；约束无解时返回 HTTP 409 与结构化冲突见证，不输出区间。

请求体（矩阵最多 12×12；`null` 为遮盖格，其余为非负整数）：

```json
{
  "matrix": [
    [5, null, null],
    [null, 3, null],
    [null, null, 2]
  ],
  "row_totals": [12, 10, 8],
  "col_totals": [9, 9, 12]
}
```

算法：先用自实现的 Dinic 最大流判定可行性并给出 Hall 型最小割见证，再用
HiGHS 线性规划逐格求上下界；运输问题约束矩阵全幺模，整数解自然成立。

## 本地开发（无 Docker 时）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/python -m pytest tests -q
```
