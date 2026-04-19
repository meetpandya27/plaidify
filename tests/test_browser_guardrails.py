import os

import pytest

from src.core.browser_pool import BrowserPool, PooledContext
from src.core.read_only_policy import ExecutionPhase, ReadOnlyExecutionPolicy


class FakeDownload:
    def __init__(self, *, url: str = "https://example.test/statement.pdf", filename: str = "statement.pdf"):
        self.url = url
        self.suggested_filename = filename
        self.cancelled = False
        self.saved_path = None

    async def cancel(self) -> None:
        self.cancelled = True

    async def save_as(self, path: str) -> None:
        self.saved_path = path
        with open(path, "wb") as handle:
            handle.write(b"pdf-bytes")


class FakePage:
    def __init__(self):
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_read_phase_download_is_captured(tmp_path):
    pool = BrowserPool()
    pool._download_root = str(tmp_path)
    pool._allow_read_downloads = True

    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)
    pooled = PooledContext(
        context=None,
        session_id="session-download",
        read_only_policy=policy,
        download_dir=str(tmp_path),
    )
    download = FakeDownload()

    await pool._handle_download(download, pooled)

    assert download.cancelled is False
    assert len(pooled.downloads) == 1
    assert pooled.downloads[0]["filename"] == "statement.pdf"
    assert pooled.downloads[0]["size_bytes"] == len(b"pdf-bytes")
    assert os.path.exists(download.saved_path)


@pytest.mark.asyncio
async def test_non_read_phase_download_is_blocked(tmp_path):
    pool = BrowserPool()
    pool._download_root = str(tmp_path)

    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.AUTH)
    pooled = PooledContext(
        context=None,
        session_id="session-auth",
        read_only_policy=policy,
        download_dir=str(tmp_path),
    )
    download = FakeDownload(filename="auth.pdf")

    await pool._handle_download(download, pooled)

    assert download.cancelled is True
    assert pooled.downloads == []
    assert policy.blocked_actions[-1].action == "download"


@pytest.mark.asyncio
async def test_extra_page_is_closed_in_read_phase():
    pool = BrowserPool()
    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)
    pooled = PooledContext(context=None, session_id="session-popup", read_only_policy=policy)
    page = FakePage()

    await pool._close_extra_page(page, pooled)

    assert page.closed is True
    assert policy.blocked_actions[-1].action == "popup"
