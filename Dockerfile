FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.13-slim

COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

COPY app.py lexicon.py fetch_data.py ./
COPY static/ static/
# Dictionaries (WikDict, Moby, Open English WordNet) are baked in: the image is self-contained.
RUN uv run python fetch_data.py

ENV STORE_DIR=/store
VOLUME ["/store"]
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
