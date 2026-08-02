"""Remote tenants cannot access or share the Cloud Run filesystem."""

from __future__ import annotations

import base64
from unittest.mock import patch

import pytest

from tp_mcp.client.context import cloud_request_context
from tp_mcp.client.http import RawResponse
from tp_mcp.server import _TOOLS_BY_NAME, _remote_safe_tool_schema
from tp_mcp.tools.analyze import _save_analysis_json
from tp_mcp.tools.workout_files import tp_download_workout_file, tp_upload_workout_file


@pytest.mark.asyncio
async def test_remote_upload_rejects_server_file_path() -> None:
    with cloud_request_context("google-alice", "alice-cookie"):
        result = await tp_upload_workout_file("123", file_path="/etc/passwd")

    assert result["error_code"] == "REMOTE_PATH_DISABLED"
    assert "/etc/passwd" not in result["message"]


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


def test_remote_tool_schemas_hide_filesystem_arguments() -> None:
    upload = _remote_safe_tool_schema(_TOOLS_BY_NAME["tp_upload_workout_file"])
    download = _remote_safe_tool_schema(_TOOLS_BY_NAME["tp_download_workout_file"])

    assert "file_path" not in upload.input_schema["properties"]
    assert "file_data_base64" in upload.input_schema["properties"]
    assert "output_path" not in download.input_schema["properties"]


def test_remote_analysis_persistence_is_fail_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("tp_mcp.tools.analyze.ANALYSIS_DATA_DIR", tmp_path)
    with cloud_request_context("google-alice", "alice-cookie"), pytest.raises(RuntimeError, match="must not"):
        _save_analysis_json(123, {"private": "alice"})

    assert not list(tmp_path.iterdir())
