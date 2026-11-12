FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 BYTE_DATA_DIR=/data BYTE_HOST=0.0.0.0 BYTE_PORT=8765
RUN useradd --create-home --uid 10001 byte && mkdir -p /data && chown byte:byte /data
WORKDIR /app
COPY pyproject.toml ./
COPY byte_agent ./byte_agent
RUN pip install --no-cache-dir .
USER byte
VOLUME ["/data"]
EXPOSE 8765
CMD ["python", "-m", "byte_agent"]
