FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Where FastMCP keeps OAuth client registrations (encrypted). Mounted as a volume.
    FASTMCP_HOME=/data \
    FIRMA_HOST=0.0.0.0 \
    FIRMA_PORT=8600

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 --home-dir /data --create-home firma \
    && chown firma:firma /data

USER firma
VOLUME /data
EXPOSE 8600

CMD ["python", "-m", "firma_mcp"]
