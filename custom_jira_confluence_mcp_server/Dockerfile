# syntax=docker/dockerfile:1

FROM python:3.11-slim

# Prevent .pyc files and ensure logs flush immediately
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps (kept minimal)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching
COPY requirements.txt ./

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy your server code
COPY . .

# Create a non-root user
RUN useradd -m appuser
USER appuser

# Azure Container Apps ingress should target this port
EXPOSE 8000

# IMPORTANT:
# Use FastMCP CLI so we can force host=0.0.0.0 inside the container.
# Also use transport=http (Streamable HTTP) as recommended in FastMCP docs. :contentReference[oaicite:1]{index=1}
CMD ["sh", "-c", "fastmcp run atlassian_mcp_server_jira_confl.py --transport http --host 0.0.0.0 --port ${PORT:-8000}"]
