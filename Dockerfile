FROM python:3.14-slim AS builder

COPY pyproject.toml uv.lock shaper_sync.py /src/
RUN pip wheel --no-cache-dir --wheel-dir /wheels /src

FROM python:3.14-slim

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/*.whl && rm -rf /wheels

ENTRYPOINT ["shaper-sync"]
