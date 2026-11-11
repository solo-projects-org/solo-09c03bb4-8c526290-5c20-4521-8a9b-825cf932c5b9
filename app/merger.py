"""跨院区月报底表的流式归并。

输入是若干份 UTF-8 CSV（列名固定为 ``病例编号,乡镇,年龄组``），每份文件
内部按脱敏病例编号的字典序升序排列。本模块按 k 路归并读取，任何时刻内存
中只保留每份文件的当前记录和当前物理行，归并结果先写到磁盘临时文件；只有
全部文件读完且全部校验通过后，调用方才拿到可下载的临时文件路径。

因此：

* 上传、归并、下载都不会把整份输入或整份结果读入内存；
* 校验失败时不产生任何响应体（临时文件被删除），不会发出半份表。

校验规则：

* 表头必须是 ``病例编号,乡镇,年龄组``（允许 UTF-8 BOM 和表头字段两侧空白）；
* 每条数据恰好 3 列，字段不能为空；
* 文件内病例编号必须非降序，出现逆序立即拒绝整批；
* 同一病例编号可出现在任意多份文件中，但所有记录的乡镇、年龄组必须一致，
  完全一致时只输出一行；不一致即同号冲突，拒绝整批并列出两处来源。
"""

from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass
from typing import BinaryIO, Iterable, Iterator

HEADER = ("病例编号", "乡镇", "年龄组")
MAX_FILES = 8
_CHUNK_SIZE = 64 * 1024
# 单条物理行的上限，避免一个没有换行的超大文件把整份内容堆在内存里。
_MAX_LINE_BYTES = 8 * 1024 * 1024
_UTF8_BOM = b"\xef\xbb\xbf"

_CONFLICT_STATUS = 409
_DEFAULT_STATUS = 422


@dataclass(frozen=True)
class SourceRef:
    """一条记录在原始上传中的位置（表头为第 1 行）。"""

    file: str
    upload_index: int
    line: int


@dataclass(frozen=True)
class Record:
    case_id: str
    township: str
    age_group: str
    source: SourceRef


class MergeError(Exception):
    """整批退回时携带来源文件与原始行号。"""

    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        file: str | None = None,
        line: int | None = None,
        sources: list[dict] | None = None,
        status_code: int | None = None,
        extra: dict | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.file = file
        self.line = line
        self.sources = sources
        self.status_code = status_code or (
            _CONFLICT_STATUS if error_type == "duplicate_conflict" else _DEFAULT_STATUS
        )
        self.extra = extra or {}

    def to_payload(self) -> dict:
        """同时给出结构化的 ``error(s)`` 与顶层定位字段，便于客户端消费。"""
        error: dict = {"type": self.error_type, "message": self.message}
        if self.file is not None:
            error["file"] = self.file
        if self.line is not None:
            error["line"] = self.line
        if self.sources is not None:
            error["sources"] = self.sources

        payload: dict = {
            "status": "rejected",
            "error_type": self.error_type,
            "detail": self.message,
            "message": self.message,
            "error": error,
            "errors": [error],
        }
        if self.file is not None:
            payload.update(
                {
                    "file": self.file,
                    "filename": self.file,
                    "source_file": self.file,
                }
            )
        if self.line is not None:
            payload.update({"line": self.line, "line_number": self.line})
        if self.sources is not None:
            payload["sources"] = self.sources
        payload.update(self.extra)
        return payload


def _record_source(record: Record) -> dict:
    return {
        "file": record.source.file,
        "upload_index": record.source.upload_index,
        "line": record.source.line,
        "case_id": record.case_id,
        "township": record.township,
        "age_group": record.age_group,
    }


class _PhysicalLineIterator(Iterator[str]):
    """按字节流逐行读取，精确维护物理行号，并做严格 UTF-8 解码。

    这里按 ``\\n`` 切分原始字节。UTF-8 的多字节序列不会包含 ``0x0A``，因此
    先切行再逐行解码既安全又能把解码错误定位到准确的行号。缓冲区只保留尚未
    遇到换行的当前行（受 ``_MAX_LINE_BYTES`` 限制）。
    """

    def __init__(self, stream: BinaryIO, file: str):
        self._stream = stream
        self._file = file
        self._buffer = b""
        self._eof = False
        self.next_line_no = 1

    def __iter__(self) -> "_PhysicalLineIterator":
        return self

    def __next__(self) -> str:
        line_no = self.next_line_no
        while True:
            newline_at = self._buffer.find(b"\n")
            if newline_at >= 0:
                raw = self._buffer[: newline_at + 1]
                self._buffer = self._buffer[newline_at + 1 :]
                break
            if self._eof:
                if self._buffer:
                    raw = self._buffer
                    self._buffer = b""
                    break
                raise StopIteration
            chunk = self._stream.read(_CHUNK_SIZE)
            if chunk:
                self._buffer += chunk
                if b"\n" not in self._buffer and len(self._buffer) > _MAX_LINE_BYTES:
                    raise MergeError(
                        "format_error",
                        (
                            f'文件 "{self._file}" 第 {line_no} 行超过 '
                            f"{_MAX_LINE_BYTES} 字节，拒绝读取"
                        ),
                        file=self._file,
                        line=line_no,
                    )
            else:
                self._eof = True

        if len(raw) > _MAX_LINE_BYTES:
            raise MergeError(
                "format_error",
                f'文件 "{self._file}" 第 {line_no} 行超过 {_MAX_LINE_BYTES} 字节，拒绝读取',
                file=self._file,
                line=line_no,
            )
        if line_no == 1 and raw.startswith(_UTF8_BOM):
            raw = raw[len(_UTF8_BOM) :]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MergeError(
                "format_error",
                f'文件 "{self._file}" 第 {line_no} 行不是有效的 UTF-8 编码',
                file=self._file,
                line=line_no,
                extra={"encoding_error": str(exc)},
            ) from exc

        self.next_line_no = line_no + 1
        return text


class _RecordIterator(Iterator[Record]):
    """把一个已排序的 CSV 文件变成逐条校验的记录流。"""

    def __init__(self, stream: BinaryIO, file: str, upload_index: int):
        self._file = file
        self._upload_index = upload_index
        self._lines = _PhysicalLineIterator(stream, file)
        self._reader = csv.reader(self._lines, strict=True)
        self._header_read = False
        self._previous: Record | None = None

    def __iter__(self) -> "_RecordIterator":
        return self

    def __next__(self) -> Record:
        while True:
            # 下一次 next(reader) 将从这个物理行开始读取；csv.reader 可能为
            # 跨行引号字段继续消费后续物理行，但记录仍归属于起始行。
            start_line = self._lines.next_line_no
            try:
                row = next(self._reader)
            except StopIteration:
                if not self._header_read:
                    raise MergeError(
                        "format_error",
                        f'文件 "{self._file}" 为空，缺少表头',
                        file=self._file,
                        line=1,
                    )
                raise
            except csv.Error as exc:
                raise MergeError(
                    "format_error",
                    (
                        f'文件 "{self._file}" 第 {start_line} 行 CSV 格式错误：'
                        f"{exc}"
                    ),
                    file=self._file,
                    line=start_line,
                ) from exc

            if not self._header_read:
                self._validate_header(row, start_line)
                self._header_read = True
                continue

            if len(row) != 3:
                raise MergeError(
                    "format_error",
                    (
                        f'文件 "{self._file}" 第 {start_line} 行应恰好包含 3 列，'
                        f"实际为 {len(row)} 列"
                    ),
                    file=self._file,
                    line=start_line,
                )
            if any(not field.strip() for field in row):
                raise MergeError(
                    "format_error",
                    f'文件 "{self._file}" 第 {start_line} 行存在空字段',
                    file=self._file,
                    line=start_line,
                )

            record = Record(
                case_id=row[0],
                township=row[1],
                age_group=row[2],
                source=SourceRef(self._file, self._upload_index, start_line),
            )
            if self._previous is not None and record.case_id < self._previous.case_id:
                raise MergeError(
                    "out_of_order",
                    (
                        f'文件 "{self._file}" 第 {start_line} 行病例编号 '
                        f'"{record.case_id}" 小于上一行第 '
                        f"{self._previous.source.line} 行的 "
                        f'"{self._previous.case_id}"，文件未按编号升序排列'
                    ),
                    file=self._file,
                    line=start_line,
                    sources=[
                        _record_source(record),
                        _record_source(self._previous),
                    ],
                )
            self._previous = record
            return record

    def _validate_header(self, row: list[str], line_no: int) -> None:
        normalized = tuple(field.strip() for field in row)
        if normalized != HEADER:
            raise MergeError(
                "format_error",
                (
                    f'文件 "{self._file}" 第 {line_no} 行表头格式错误：'
                    f"应为 {','.join(HEADER)}，实际为 {','.join(row)}"
                ),
                file=self._file,
                line=line_no,
            )


class _Cursor:
    """单个文件的当前记录，初始化和前进时都会做文件内排序校验。"""

    __slots__ = ("_records", "current")

    def __init__(self, stream: BinaryIO, file: str, upload_index: int):
        self._records = _RecordIterator(stream, file, upload_index)
        self.current: Record | None = next(self._records, None)

    def advance(self) -> None:
        self.current = next(self._records, None)


def _merge_into(
    sources: Iterable[tuple[str, int, BinaryIO]],
    output_path: str,
) -> None:
    cursors = [
        _Cursor(stream, file, upload_index)
        for file, upload_index, stream in sources
    ]

    with open(output_path, "w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(HEADER)

        while True:
            active = [cursor for cursor in cursors if cursor.current is not None]
            if not active:
                break

            target = min(cursor.current.case_id for cursor in active if cursor.current)
            first: Record | None = None

            for cursor in active:
                # 同一编号在一个文件内也可能连续出现；全部取出并逐条核对，
                # 但只保留“第一处”作为输出/冲突参照，避免重复编号堆积内存。
                while cursor.current is not None and cursor.current.case_id == target:
                    record = cursor.current
                    if first is None:
                        first = record
                    elif (
                        record.township != first.township
                        or record.age_group != first.age_group
                    ):
                        raise MergeError(
                            "duplicate_conflict",
                            (
                                f'病例编号 "{target}" 在文件 '
                                f'"{first.source.file}" 第 {first.source.line} 行与文件 '
                                f'"{record.source.file}" 第 {record.source.line} 行'
                                "的乡镇或年龄组不一致"
                            ),
                            file=record.source.file,
                            line=record.source.line,
                            sources=[
                                _record_source(first),
                                _record_source(record),
                            ],
                        )
                    cursor.advance()

            if first is not None:
                writer.writerow([first.case_id, first.township, first.age_group])


def merge_uploads(sources: Iterable[tuple[str, int, BinaryIO]]) -> str:
    """归并若干上传流，返回写好的 UTF-8 CSV 临时文件路径。

    ``sources`` 的每一项是 ``(展示用的文件名, 1 起的上传序号, 二进制流)``。
    调用方负责在请求结束后删除返回的临时文件。
    """
    source_list = list(sources)
    if not source_list:
        raise MergeError(
            "empty_batch",
            "至少需要上传 1 份 CSV 文件",
            extra={"max_files": MAX_FILES},
        )
    if len(source_list) > MAX_FILES:
        raise MergeError(
            "too_many_files",
            f"一次最多接收 {MAX_FILES} 份 CSV 文件，本次收到 {len(source_list)} 份",
            extra={
                "max_files": MAX_FILES,
                "uploaded_files": [item[0] for item in source_list],
            },
        )

    fd, output_path = tempfile.mkstemp(prefix="merged-cases-", suffix=".csv")
    os.close(fd)
    try:
        for _, _, stream in source_list:
            try:
                stream.seek(0)
            except (AttributeError, OSError):
                pass
        _merge_into(source_list, output_path)
    except BaseException:
        safe_unlink(output_path)
        raise
    return output_path


def safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
