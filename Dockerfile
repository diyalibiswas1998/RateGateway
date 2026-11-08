# The Python version matches .python-version.
FROM python:3.12-slim

# uv, copied from its official image (pinned to the version used to write uv.lock).
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

WORKDIR /app

# Use the image's Python instead of downloading one; copy packages into .venv
# rather than linking them, and precompile them so the first request is fast.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Dependencies first: this layer is rebuilt only when the lock file changes,
# not on every code change.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

COPY app ./app

# Run the virtual environment's programs directly, with no uv at runtime.
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# --proxy-headers: take the client's IP from nginx's X-Forwarded-For header.
# --forwarded-allow-ips "*": trust that header from any address, which is safe
# only because the API port is never published; nginx is the only way in.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
