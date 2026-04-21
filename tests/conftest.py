"""Shared pytest fixtures."""

import pytest


@pytest.fixture
def anyio_backend():
    """Force pytest-asyncio to use asyncio (not trio)."""
    return "asyncio"
