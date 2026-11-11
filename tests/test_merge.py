"""归并接口测试：跨院区去重、排序、整批退回与来源行号。"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app import merger
from app.main import app

client = TestClient(app)
HEADER = "病例编号,乡镇,年龄组\n"
MERGE_PATHS = [
    "/api/v1/merge",
    "/api/v1/merge-cases",
    "/api/v1/cases/merge",
    "/api/v1/merge-records",
    "/api/v1/case-merge",
    "/merge",
]


def _upload(name: str, text: str) -> tuple[str, tuple[str, bytes, str]]:
    return ("files", (name, text.encode("utf-8"), "text/csv"))


def _post(contents, path: str = "/api/v1/merge"):
    return client.post(
        path,
        files=[_upload(name, text) for name, text in contents],
    )


def test_bundled_samples_merge_deduplicates_and_sorts():
    with open("samples/campus_a.csv", "rb") as a, open(
        "samples/campus_b.csv", "rb"
    ) as b:
        response = client.post(
            "/api/v1/merge",
            files=[
                ("files", ("campus_a.csv", a, "text/csv")),
                ("files", ("campus_b.csv", b, "text/csv")),
            ],
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = response.content.decode("utf-8").splitlines()
    assert rows[0] == "病例编号,乡镇,年龄组"
    assert rows[1:] == [
        "C20260001,东城镇,0-14岁",
        "C20260002,南山镇,15-64岁",
        "C20260003,西城镇,15-64岁",
        "C20260005,南山镇,65岁以上",
        "C20260007,北河镇,15-64岁",
        "C20260008,西城镇,0-14岁",
        "C20260010,东城镇,65岁以上",
    ]


def test_same_id_same_fields_merges_one_row():
    response = _post(
        [
            ("a.csv", HEADER + "A,东,0\nC,南,1\n"),
            ("b.csv", HEADER + "A,东,0\nB,西,0\nC,南,1\n"),
        ]
    )
    assert response.status_code == 200
    assert response.content.decode("utf-8").splitlines() == [
        "病例编号,乡镇,年龄组",
        "A,东,0",
        "B,西,0",
        "C,南,1",
    ]


def test_duplicate_conflict_returns_409_and_both_sources():
    response = _post(
        [
            ("a.csv", HEADER + "A,东,0\n"),
            ("b.csv", HEADER + "A,东,1\n"),
        ]
    )
    assert response.status_code == 409
    body = response.json()
    assert body["status"] == "rejected"
    assert body["error_type"] == "duplicate_conflict"
    assert body["file"] == "b.csv"
    assert body["line"] == 2
    assert len(body["sources"]) == 2
    assert {source["file"] for source in body["sources"]} == {"a.csv", "b.csv"}
    assert {source["line"] for source in body["sources"]} == {2}
    # 出错时不返回半份 CSV
    assert not response.headers["content-type"].startswith("text/csv")
    assert body["status"] == "rejected"


def test_out_of_order_returns_422_with_file_and_original_line():
    response = _post([("a.csv", HEADER + "B,东,0\nA,西,1\n")])
    assert response.status_code == 422
    body = response.json()
    assert body["error_type"] == "out_of_order"
    assert body["file"] == "a.csv"
    assert body["line"] == 3
    assert body["error"]["line"] == 3


@pytest.mark.parametrize(
    "text, expected_line, error_type",
    [
        ("x,y,z\nA,东,0\n", 1, "format_error"),
        (HEADER + "A,东,0,extra\n", 2, "format_error"),
        (HEADER + "A,,0\n", 2, "format_error"),
        (HEADER + "A,东,0\n\nB,西,1\n", 3, "format_error"),
        ("", 1, "format_error"),
    ],
)
def test_format_errors_point_to_file_and_line(text, expected_line, error_type):
    response = _post([("bad.csv", text)])
    assert response.status_code == 422
    body = response.json()
    assert body["error_type"] == error_type
    assert body["file"] == "bad.csv"
    assert body["line"] == expected_line


def test_invalid_utf8_reports_original_line():
    data = HEADER.encode("utf-8") + b"A,\xff,0\n"
    response = client.post(
        "/api/v1/merge",
        files=[("files", ("bad.csv", data, "text/csv"))],
    )
    assert response.status_code == 422
    body = response.json()
    assert body["file"] == "bad.csv"
    assert body["line"] == 2
    assert body["error_type"] == "format_error"


def test_too_many_files_returns_422():
    response = _post(
        [(f"f{i}.csv", HEADER + f"A{i},东,0\n") for i in range(9)]
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error_type"] == "too_many_files"
    assert body["max_files"] == 8


def test_all_compatibility_paths_work():
    for path in MERGE_PATHS:
        response = _post([("a.csv", HEADER + "A,东,0\n")], path=path)
        assert response.status_code == 200, path


def test_single_file_field_alias_is_accepted():
    response = client.post(
        "/api/v1/merge",
        files=[("file", ("a.csv", HEADER + "A,东,0\n", "text/csv"))],
    )
    assert response.status_code == 200


def test_bom_crlf_and_quoted_fields_are_supported():
    text = "﻿病例编号,乡镇,年龄组\r\nA,\"东,城\",0\r\nB,西,1\r\n"
    response = _post([("a.csv", text)])
    assert response.status_code == 200
    assert response.content.decode("utf-8").splitlines() == [
        "病例编号,乡镇,年龄组",
        'A,"东,城",0',
        "B,西,1",
    ]


def test_merger_does_not_leave_temp_file_on_error(tmp_path, monkeypatch):
    # 把临时目录指到测试目录，确认出错后没有遗留结果文件
    monkeypatch.setattr(merger.tempfile, "tempdir", str(tmp_path))
    with pytest.raises(merger.MergeError):
        merger.merge_uploads(
            [
                ("a.csv", 1, io.BytesIO((HEADER + "B,东,0\nA,西,1\n").encode())),
            ]
        )
    assert list(tmp_path.iterdir()) == []
