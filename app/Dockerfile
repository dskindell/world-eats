FROM python:3.12-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PORT=8080

COPY *.py ./
COPY static ./static

EXPOSE 8080
CMD ["python", "server.py"]
