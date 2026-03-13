"""FastAPI web application — dashboard API and SSE stream."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import storage.db as db

logger = logging.getLogger(__name__)

app = FastAPI(title="Internet Schminternet", docs_url=None, redoc_url=None)

templates = Jinja2Templates(directory="web/templates")
app.mount("/static", StaticFiles(directory="web/static"), name="static")

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
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/status")
async def api_status() -> list[dict]:
    return await db.get_current_status()


@app.get("/api/metrics/{monitor}")
async def api_metrics(monitor: str, hours: int = 24) -> list[dict]:
    return await db.query_recent(monitor, hours)


@app.get("/api/events")
async def api_events(limit: int = 50) -> list[dict]:
    return await db.get_events(limit)


@app.get("/partials/events", response_class=HTMLResponse)
async def partial_events(request: Request, limit: int = 50) -> HTMLResponse:
    events = await db.get_events(limit)
    return templates.TemplateResponse(
        "partials/events.html", {"request": request, "events": events}
    )


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
