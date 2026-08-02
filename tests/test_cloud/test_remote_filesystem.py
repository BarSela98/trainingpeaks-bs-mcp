"""Remote tenants cannot access or share the Cloud Run filesystem."""

from __future__ import annotations

import base64
import gzip
from unittest.mock import patch

import pytest

from tp_mcp.client.context import cloud_request_context
from tp_mcp.client.http import APIResponse, RawResponse
from tp_mcp.server import _TOOLS_BY_NAME, _remote_safe_tool_schema
from tp_mcp.tools.analyze import _save_analysis_json
from tp_mcp.tools.workout_files import REMOTE_MAX_FILE_BYTES, tp_download_workout_file, tp_upload_workout_file


@pytest.mark.asyncio
async def test_remote_upload_rejects_server_file_path() -> None:
    with cloud_request_context("google-alice", "alice-cookie"):
        result = await tp_upload_workout_file("123", file_path="/etc/passwd")

    assert result["error_code"] == "REMOTE_PATH_DISABLED"
    assert "/etc/passwd" not in result["message"]


@pytest.mark.asyncio
async def test_remote_upload_accepts_base64_without_reading_server_files() -> None:
    uploaded: dict = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def ensure_athlete_id(self):
            return 42

        async def post(self, endpoint, *, json):
            uploaded["endpoint"] = endpoint
            uploaded["json"] = json
            return APIResponse(success=True, data={"workoutId": 123})

    raw_file = b"private-fit-data"
    with (
        cloud_request_context("google-alice", "alice-cookie"),
        patch("tp_mcp.tools.workout_files.TPClient", return_value=FakeClient()),
        patch("pathlib.Path.read_bytes") as read_file,
    ):
        result = await tp_upload_workout_file(
            "123",
            file_data_base64=base64.b64encode(raw_file).decode("ascii"),
            workout_day="2026-08-02",
        )

    read_file.assert_not_called()
    assert result["workout_id"] == "123"
    assert result["uploaded_bytes"] == len(raw_file)
    assert uploaded["endpoint"] == "/fitness/v6/athletes/42/workouts/123/filedata"
    assert uploaded["json"]["workoutDay"] == "2026-08-02T00:00:00"
    assert gzip.decompress(base64.b64decode(uploaded["json"]["data"])) == raw_file


@pytest.mark.asyncio
async def test_remote_upload_rejects_decoded_data_over_limit_before_api_call() -> None:
    encoded = base64.b64encode(b"x" * (REMOTE_MAX_FILE_BYTES + 1)).decode("ascii")
    with (
        cloud_request_context("google-alice", "alice-cookie"),
        patch("tp_mcp.tools.workout_files.TPClient") as client,
    ):
        result = await tp_upload_workout_file("123", file_data_base64=encoded, workout_day="2026-08-02")

    client.assert_not_called()
    assert result["error_code"] == "VALIDATION_ERROR"
    assert str(REMOTE_MAX_FILE_BYTES) in result["message"]


@pytest.mark.asyncio
async def test_remote_download_returns_base64_without_writing() -> None:
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def ensure_athlete_id(self):
            return 42

        async def get_raw(self, endpoint):
            return RawResponse(
                success=True,
                content=b"private-fit-data",
                content_type="application/octet-stream",
                content_disposition='attachment; filename="ride.fit.gz"',
            )

    with (
        cloud_request_context("google-alice", "alice-cookie"),
        patch("tp_mcp.tools.workout_files.TPClient", return_value=FakeClient()),
        patch("tp_mcp.tools.workout_files._save_workout_file") as save_file,
    ):
        result = await tp_download_workout_file("123", "-7")

    save_file.assert_not_called()
    assert result["saved_to"] is None
    assert base64.b64decode(result["file_data_base64"]) == b"private-fit-data"


@pytest.mark.asyncio
async def test_remote_download_rejects_output_path() -> None:
    with cloud_request_context("google-alice", "alice-cookie"):
        result = await tp_download_workout_file("123", "7", output_path="/tmp/shared.fit")

    assert result["error_code"] == "REMOTE_PATH_DISABLED"


@pytest.mark.asyncio
async def test_remote_download_rejects_data_over_limit_without_writing() -> None:
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def ensure_athlete_id(self):
            return 42

        async def get_raw(self, endpoint):
            return RawResponse(success=True, content=b"x" * (REMOTE_MAX_FILE_BYTES + 1))

    with (
        cloud_request_context("google-alice", "alice-cookie"),
        patch("tp_mcp.tools.workout_files.TPClient", return_value=FakeClient()),
        patch("tp_mcp.tools.workout_files._save_workout_file") as save_file,
    ):
        result = await tp_download_workout_file("123", "7")

    save_file.assert_not_called()
    assert result["error_code"] == "FILE_TOO_LARGE"
    assert str(REMOTE_MAX_FILE_BYTES) in result["message"]


def test_remote_tool_schemas_hide_filesystem_arguments() -> None:
    upload = _remote_safe_tool_schema(_TOOLS_BY_NAME["tp_upload_workout_file"])
    download = _remote_safe_tool_schema(_TOOLS_BY_NAME["tp_download_workout_file"])

    assert "file_path" not in upload.input_schema["properties"]
    assert "file_data_base64" in upload.input_schema["properties"]
    assert "output_path" not in download.input_schema["properties"]


def test_remote_tool_descriptions_match_stateless_file_behavior() -> None:
    analyze = _remote_safe_tool_schema(_TOOLS_BY_NAME["tp_analyze_workout"])
    upload = _remote_safe_tool_schema(_TOOLS_BY_NAME["tp_upload_workout_file"])
    download = _remote_safe_tool_schema(_TOOLS_BY_NAME["tp_download_workout_file"])

    assert "does not save or return a server-side JSON file" in analyze.description
    assert "base64-encoded bytes" in upload.description
    assert "cannot read server file paths" in upload.description
    assert "3 MiB" in upload.description
    assert "base64-encoded bytes" in download.description
    assert "does not write server files" in download.description
    assert "3 MiB" in download.description
    assert "Saves full time-series to JSON file" in _TOOLS_BY_NAME["tp_analyze_workout"].description


def test_remote_analysis_persistence_is_fail_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("tp_mcp.tools.analyze.ANALYSIS_DATA_DIR", tmp_path)
    with cloud_request_context("google-alice", "alice-cookie"), pytest.raises(RuntimeError, match="must not"):
        _save_analysis_json(123, {"private": "alice"})

    assert not list(tmp_path.iterdir())
