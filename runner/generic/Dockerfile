FROM python:3.12-slim

# Node + the Claude Code CLI for the Claude backends (subscription token or
# Anthropic key); the other backends run through LiteLLM.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /runner
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py orchestrator.txt ./

# Claude Code keeps its state under $HOME — a mounted volume keeps it.
ENV HOME=/config PYTHONUNBUFFERED=1

CMD ["python", "agent.py"]
