#!/usr/bin/env python3
"""Command line front end for the QLC+ Agent API.

The implementation lives in ``qlcplus_mcp/cli.py``; this file exists so the CLI
can be run as ``./cli.py ...`` from the project root without installing anything,
and so the entry point is where the brief asked for it.

    ./cli.py tools
    ./cli.py call getState
    ./cli.py --help
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qlcplus_mcp.cli import build_parser, main  # noqa: E402

__all__ = ["main", "build_parser"]

if __name__ == "__main__":
    raise SystemExit(main())
