FROM python:3.12-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    DATA_DIR=/data

# Fixed non-root user; /data is created owned by it so a fresh named volume is writable.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app \
    && mkdir /data && chown 10001:10001 /data

COPY *.py ./
COPY static ./static

USER 10001:10001
EXPOSE 8080
CMD ["python", "server.py"]
