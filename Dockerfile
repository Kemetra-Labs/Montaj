FROM ghcr.io/astral-sh/uv:0.13.3 AS uv
FROM python:3.12-slim-trixie

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fontconfig \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 montaj \
    && useradd --uid 10001 --gid montaj --create-home montaj \
    && install -d -o montaj -g montaj /app/data

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-build --no-install-project

COPY README.md LICENSE ./
COPY src/ ./src/
RUN uv build --wheel --no-sources --out-dir /tmp/montaj-dist \
    && uv pip install --no-build --no-deps /tmp/montaj-dist/*.whl yt-dlp==2026.10.26 \
    && rm -rf /tmp/montaj-dist \
    && install -D -m 644 src/montaj/assets/fonts/NotoSansArabic-Regular.ttf \
        /usr/local/share/fonts/montaj/NotoSansArabic-Regular.ttf \
    && fc-cache -f /usr/local/share/fonts/montaj

COPY config/ ./config/
COPY prompts/ ./prompts/
COPY docs/server.md ./docs/server.md
COPY tests/ ./tests/

USER montaj
ENTRYPOINT ["montaj"]
CMD ["--help"]
