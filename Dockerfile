# Internet Schminternet — headless container image.
#
# LEDs are Pi-only: rpi-ws281x is not installed here and leds.enabled defaults
# to false, so the controller no-ops. This image gives you the monitors, the
# SQLite history, the dashboard and email alerts.

FROM python:3.13-slim

# monitors/ping.py shells out to the system ping binary.
RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Dependency layer — rebuilt only when the lockfile changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project

COPY . .

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# The dashboard binds 0.0.0.0:8080 inside the container (web.port in config).
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/status', timeout=5)" || exit 1

# main.py installs SIGTERM/SIGINT handlers: the scheduler stops, shutdown_at is
# written to the state table, and uvicorn exits cleanly.
CMD ["python", "main.py"]
