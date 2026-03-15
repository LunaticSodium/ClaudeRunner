# claude-runner-base
# Minimal Debian image with pinned Node.js LTS + Claude Code
# Built by: claude-runner docker update
#
# Build:
#   docker build -t claude-runner-base:latest .
#   docker build --build-arg CLAUDE_CODE_VERSION=1.2.3 -t claude-runner-base:1.2.3 .

FROM debian:bookworm-slim

# ── System dependencies ────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js 20 LTS via NodeSource ─────────────────────────────────────────
# Pin to Node 20 (Active LTS). Update deliberately after testing — not
# automatically — so that Claude Code compatibility is verified first.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Sanity-check that node + npm are present and print versions to the build log.
RUN node --version && npm --version

# ── Claude Code ───────────────────────────────────────────────────────────
# Update CLAUDE_CODE_VERSION deliberately after testing, not automatically.
# Pass --build-arg CLAUDE_CODE_VERSION=x.y.z to pin to a specific release.
ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && claude --version

# ── Workspace setup ───────────────────────────────────────────────────────
# Create the workspace and progress-log directory before Claude Code runs so
# that directory ownership is correct even when running as a non-root user.
RUN mkdir -p /workspace/.claude-runner
WORKDIR /workspace

# ── Non-root user ─────────────────────────────────────────────────────────
# Running as a non-root user provides an extra layer of defence-in-depth even
# inside the already-isolated container.
RUN useradd -m -u 1000 -s /bin/bash claude \
    && chown -R claude:claude /workspace

USER claude

# Verify Claude Code is accessible as the non-root user.
RUN claude --version

CMD ["bash"]
