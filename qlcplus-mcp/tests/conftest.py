"""Shared fixtures. The suite never touches a real QLC+ install."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fake_server as fake_server_module  # noqa: E402  (tests dir is on sys.path)
from fake_server import FakeQlcServer  # noqa: E402
from qlcplus_mcp.client import QlcAgentClient  # noqa: E402

__all__ = ["FakeQlcServer", "fake_server_module"]


@pytest_asyncio.fixture
async def make_server():
    """Factory for fake servers with custom handlers; stops them at teardown."""
    created: list[FakeQlcServer] = []

    def _make(**kwargs):
        instance = FakeQlcServer(**kwargs)
        created.append(instance)
        return instance

    yield _make

    for instance in created:
        if instance._server is not None:  # noqa: SLF001 - test teardown
            await instance.stop()


@pytest_asyncio.fixture
async def server() -> AsyncIterator[FakeQlcServer]:
    """A running fake QLC+ on a random port, echoing every request back."""
    instance = FakeQlcServer()
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


@pytest_asyncio.fixture
async def client(server: FakeQlcServer) -> AsyncIterator[QlcAgentClient]:
    """A connected client pointed at the fake server, with a fast timeout."""
    instance = QlcAgentClient(uri=server.uri, timeout=2.0)
    await instance.connect()
    try:
        yield instance
    finally:
        await instance.close()
