FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCARLETWATCH_DATA_DIR=/data \
    SCARLETWATCH_HOST=0.0.0.0 \
    SCARLETWATCH_SUPPRESS_BIND_WARNING=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && useradd --system --uid 10001 --create-home scarletwatch \
    && mkdir /data && chown scarletwatch /data

USER scarletwatch
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["snipe", "run"]
