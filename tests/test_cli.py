"""Tests for the tp-mcp command-line interface."""

from __future__ import annotations

import json
import sys

from tp_mcp import cli


def test_config_uses_running_frozen_executable(monkeypatch, capsys) -> None:
    executable = "/opt/trainingpeaks/tp-mcp-macos-arm64"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", executable)

    assert cli.cmd_config() == 0

    output = capsys.readouterr().out
    config = json.loads(output.split("\n\n", maxsplit=1)[1])
    assert config["trainingpeaks"] == {"command": executable, "args": ["serve"]}


def test_help_does_not_advertise_removed_cloud_connect(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["tp-mcp", "help"])

    assert cli.main() == 0
    assert "cloud-connect" not in capsys.readouterr().out


def test_cloud_connect_command_is_removed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["tp-mcp", "cloud-connect"])

    assert cli.main() == 1
    assert "Unknown command: cloud-connect" in capsys.readouterr().out
