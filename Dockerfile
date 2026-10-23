# The Recall MCP server over Streamable HTTP, for hosts that connect by URL.
# Any container host that terminates TLS works; it serves POST /mcp on $PORT.
# The image holds no keys — every request brings its own.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY agent_recall ./agent_recall
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 recall

USER recall
ENV HOST=0.0.0.0 PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["agent-recall-http"]
