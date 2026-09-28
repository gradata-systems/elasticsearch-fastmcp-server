# Build the virtual environment with uv, then copy it into a slim runtime image.
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.12-slim

RUN useradd --system --uid 10001 --no-create-home --home-dir /nonexistent es-mcp

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY main.py config.py access_policy.yaml ./
COPY packs/ packs/
COPY prompts/ prompts/
COPY security/ security/
COPY sources/ sources/
COPY tools/ tools/
COPY utils/ utils/

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001
EXPOSE 8000
CMD ["python", "main.py"]
