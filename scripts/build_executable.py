"""Build a self-contained tp-mcp executable with PyInstaller."""

from __future__ import annotations

import os
from pathlib import Path


def main() -> None:
    """Build the executable into the repository's dist directory."""
    root = Path(__file__).resolve().parents[1]
    entrypoint = root / "src" / "tp_mcp" / "__main__.py"
    os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(root / "build" / "pyinstaller" / "cache"))

    import PyInstaller.__main__

    PyInstaller.__main__.run(
        [
            "--noconfirm",
            "--clean",
            "--onefile",
            "--noupx",
            "--name",
            "tp-mcp",
            "--paths",
            str(root / "src"),
            "--distpath",
            str(root / "dist"),
            "--workpath",
            str(root / "build" / "pyinstaller" / "work"),
            "--specpath",
            str(root / "build" / "pyinstaller" / "spec"),
            "--collect-all",
            "tp_mcp",
            "--collect-all",
            "browser_cookie3",
            "--copy-metadata",
            "browser-cookie3",
            "--copy-metadata",
            "keyring",
            "--copy-metadata",
            "mcp",
            "--copy-metadata",
            "tp-mcp",
            str(entrypoint),
        ]
    )


if __name__ == "__main__":
    main()
