FROM python:3.12-alpine@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY sync.py /app/sync.py
RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 CMD ["python", "/app/sync.py", "--healthcheck"]
ENTRYPOINT ["python", "-u", "/app/sync.py"]
