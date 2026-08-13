# syntax=docker/dockerfile:1

# ---- build stage -------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: this layer is cached as long as the lockfile is unchanged.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-install-project --no-dev

COPY pyproject.toml uv.lock README.md ./
COPY starred/ ./starred/

# --no-editable installs the package into the venv itself, so the runtime stage
# only needs /app/.venv and not the source tree.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- runtime stage -----------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 starred

WORKDIR /app

COPY --from=builder --chown=starred:starred /app/.venv /app/.venv

USER starred

# `starred analyze` is intentionally not supported here: it needs the Claude CLI,
# which stays on the developer machine. Every other command works.
ENTRYPOINT ["starred"]
CMD ["--help"]
