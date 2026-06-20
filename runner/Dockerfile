FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Reference LLM backend: Claude Code (swap via RUNNER_CMD for another agent CLI).
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /runner
COPY orchestrator.txt run-once.sh loop.sh ./
RUN chmod +x run-once.sh loop.sh

# Claude Code stores auth + MCP config under $HOME — point it at the mounted
# /config volume so the one-time connector login + API key survive restarts.
ENV HOME=/config

ENTRYPOINT ["./loop.sh"]
