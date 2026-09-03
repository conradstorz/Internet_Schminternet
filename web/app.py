"""FastAPI web application — dashboard API and SSE stream."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import storage.db as db

logger = logging.getLogger(__name__)

app = FastAPI(title="Internet Schminternet", docs_url=None, redoc_url=None)

templates = Jinja2Templates(directory="web/templates")
app.mount("/static", StaticFiles(directory="web/static"), name="static")

_STATIC_DIR = Path("web/static")


def asset_version() -> str:
    """Cache-busting token for the static assets the page loads.

    Derived from the size and mtime of every file in web/static, so a deployed
    change produces a new script URL and the browser fetches it without the
    user having to force-reload.
    """
    stamp = []
    try:
        for path in sorted(_STATIC_DIR.rglob("*")):
            if path.is_file():
                st = path.stat()
                stamp.append(f"{path.name}:{st.st_size}:{int(st.st_mtime)}")
    except OSError:  # pragma: no cover - unreadable static dir
        return "dev"
    if not stamp:
        return "dev"
    return hashlib.sha256("|".join(stamp).encode()).hexdigest()[:12]

# ---------------------------------------------------------------------------
# Server-Sent Events fan-out
# ---------------------------------------------------------------------------

_sse_subscribers: list[asyncio.Queue] = []


def broadcast_status(data: dict) -> None:
    """Push a status update to all connected SSE clients (fire-and-forget)."""
    payload = json.dumps(data)
    for queue in list(_sse_subscribers):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    # no-store on the page itself: the asset URLs it carries are versioned, so a
    # cached page would keep pointing at the previous main.js after a deploy and
    # the dashboard would only update on a hard reload.
    return templates.TemplateResponse(
        request,
        "index.html",
        {"asset_version": asset_version()},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/status")
async def api_status() -> list[dict]:
    return await db.get_current_status()


@app.get("/api/metrics/{monitor}")
async def api_metrics(monitor: str, hours: int = 24) -> list[dict]:
    return await db.query_recent(monitor, hours)


def _parse_iso_timestamp(value: str, field: str) -> None:
    """Raise HTTP 400 if ``value`` is not a parseable ISO-8601 timestamp."""
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"{field} must be an ISO-8601 timestamp"
        )


@app.get("/api/events")
async def api_events(
    limit: int = 50,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> list[dict]:
    """Return events, either the most recent `limit` (default) or a time window.

    Passing `start` and `end` (ISO-8601 UTC) switches to a windowed, oldest-
    first read for the events timeline; `limit` alone keeps the original
    newest-first behaviour for any existing caller.
    """
    if start is not None or end is not None:
        if start is None or end is None:
            raise HTTPException(
                status_code=400, detail="start and end must both be provided"
            )
        _parse_iso_timestamp(start, "start")
        _parse_iso_timestamp(end, "end")
        return await db.get_events_range(start, end)

    return await db.get_events(limit)


@app.get("/stream")
async def sse_stream(request: Request) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _sse_subscribers.append(queue)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            try:
                _sse_subscribers.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(event_generator(), media_type="text/event-stream")
