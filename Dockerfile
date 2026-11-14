FROM python:3.15-alpine@sha256:a0f2e268f2ce8f3709611a90054ec6a8ae31f6251f5896b8648b9bb99835e30a
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY sync.py /app/sync.py
RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 CMD ["python", "/app/sync.py", "--healthcheck"]
ENTRYPOINT ["python", "-u", "/app/sync.py"]
