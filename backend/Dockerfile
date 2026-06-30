# Backend Service Dockerfile
# Multi-stage build: base stage includes all runtime dependencies
FROM python:3.13-slim AS base

# Install all system dependencies in a single layer
RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    ca-certificates \
    gnupg \
    wget \
    unzip \
    procps \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Setup Node.js repository and install Node.js 22 (LTS) and npm
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Yarn and pnpm (agents use both in generated Node workspaces)
RUN npm install -g yarn pnpm

# OpenJDK 21 and Maven (default Debian packages)
RUN apt-get update && apt-get install -y \
    openjdk-21-jdk-headless \
    maven \
    && rm -rf /var/lib/apt/lists/*

# Install Go 1.23 (latest stable)
RUN wget https://go.dev/dl/go1.23.5.linux-amd64.tar.gz \
    && tar -C /usr/local -xzf go1.23.5.linux-amd64.tar.gz \
    && rm go1.23.5.linux-amd64.tar.gz
ENV JAVA_HOME="/usr/lib/jvm/java-21-openjdk-amd64"
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# Gradle — small JVM build-tool binary, installed like the other runtimes (Go/Java).
# The heavy Android SDK (platform-tools, build-tools, NDK) is NOT baked here; it lives in
# the NFS cache and is provisioned manually via scripts/init-mobile-sdk.sh.
# Android/JVM projects normally build through the pinned wrapper (./gradlew); a system
# gradle is provided so `gradle` is always on PATH for projects that invoke it directly.
ENV GRADLE_VERSION="8.11.1"
RUN wget -q "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" -O /tmp/gradle.zip \
    && unzip -q /tmp/gradle.zip -d /opt \
    && rm /tmp/gradle.zip \
    && ln -s "/opt/gradle-${GRADLE_VERSION}" /opt/gradle
ENV PATH="/opt/gradle/bin:${PATH}"

# Kotlin command-line compiler — small binary. Android Kotlin builds run through the Gradle
# Kotlin plugin, but kotlinc is provided for standalone Kotlin/JVM tasks. The zip extracts a
# top-level kotlinc/ directory into /opt.
ENV KOTLIN_VERSION="2.1.0"
RUN wget -q "https://github.com/JetBrains/kotlin/releases/download/v${KOTLIN_VERSION}/kotlin-compiler-${KOTLIN_VERSION}.zip" -O /tmp/kotlin.zip \
    && unzip -q /tmp/kotlin.zip -d /opt \
    && rm /tmp/kotlin.zip
ENV PATH="/opt/kotlinc/bin:${PATH}"

# Point Android SDK to persistent volume cache.
# Path MUST match setup_workspace_cache_directories() in app/services/claude_code.py
# and the init-mobile-sdk.sh default — the shared SDK lives under caches/common/android.
ENV ANDROID_SDK_ROOT="/workspaces/caches/common/android"
ENV ANDROID_HOME="/workspaces/caches/common/android"
ENV ANDROID_SDK_CMDLINE_TOOLS_VERSION="11076708"
ENV PATH="${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin:${ANDROID_SDK_ROOT}/platform-tools:${PATH}"

# Flutter is provisioned PER-WORKSPACE: it self-mutates bin/cache at runtime and offers no way
# to relocate that cache, so a shared read-only copy would race across concurrent workspaces.
# init-mobile-sdk.sh installs ONE pristine TEMPLATE at the path below; the flutter/dart wrappers
# (installed in /usr/local/bin further down) copy it into each workspace's own FLUTTER_ROOT
# (set per-workspace by setup_workspace_cache_directories) on first use. Only the template path
# is baked — a per-workspace FLUTTER_ROOT/bin can't live on a static PATH, so the wrappers are
# the entrypoints and FLUTTER_ROOT itself is supplied at runtime in the agent env.
ENV FLUTTER_TEMPLATE_ROOT="/workspaces/caches/common/flutter"

# Install GitHub CLI (gh) — agents use this to trigger GHA workflows and read logs
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Backend service stage
FROM base

WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock* ./

# Install dependencies using uv
# Create virtual environment and install runtime dependencies only
# Verify venv was created
RUN uv sync --frozen --no-dev || uv sync --no-dev && \
    test -d /app/.venv && \
    test -f /app/.venv/bin/uvicorn && \
    echo "✓ Virtual environment created successfully"

# Copy application code (preserve the app directory structure)
COPY app ./app

# Create .claude directory and copy settings to root's home directory
RUN mkdir -p /root/.claude
COPY claude-settings.json /root/.claude/settings.json

# ---------------------------------------------------------------------------
# Rosetta knowledge-base plugin — bundled so KB init runs WITHOUT the ims-mcp service /
# ROSETTA_API_KEY. Plugin mode is the DEFAULT in every environment (ROSETTA_MCP_ENABLED=false,
# ROSETTA_PLUGIN_PATH=/opt/rosetta-plugin). At workspace prep time the backend copies this
# plugin's agents/skills/commands/hooks into each workspace's .claude/ (see
# WorkspaceManager.provision_rosetta_plugin); the agents discover them via
# setting_sources=["project"]. Set ROSETTA_MCP_ENABLED=true to opt back into the live MCP,
# which then ignores this path.
#
# Installed from the public griddynamics/rosetta marketplace via the `claude` CLI that ships
# bundled inside claude-agent-sdk (a self-contained native binary at
# .venv/.../claude_agent_sdk/_bundled/claude — present after `uv sync` above; the same CLI
# the codegen agents use). We invoke it by absolute path (it is not a uv/venv entry point).
# The install path is read back from the CLI's own installed_plugins.json (via jq) and copied
# to a stable /opt/rosetta-plugin so the runtime path never depends on the resolved version.
# Plugin versions cannot be pinned via the CLI, so the build takes the marketplace's current version.
#
# The guard asserts the staged plugin actually ships the offline toolset (manifest + skills +
# workflows), failing the build if the marketplace ever serves an empty/MCP-only plugin.
# ---------------------------------------------------------------------------
ENV HOME=/root
RUN CLAUDE_BIN="$(find /app/.venv -type f -path '*/claude_agent_sdk/_bundled/claude' | head -1)" \
 && test -n "$CLAUDE_BIN" \
 && "$CLAUDE_BIN" plugin marketplace add griddynamics/rosetta \
 && "$CLAUDE_BIN" plugin install "rosetta@rosetta" --scope user \
 && installed="$(jq -r '.plugins["rosetta@rosetta"][] | select(.scope=="user") | .installPath' /root/.claude/plugins/installed_plugins.json | head -1)" \
 && test -n "$installed" && test -d "$installed" \
 && cp -r "$installed" /opt/rosetta-plugin \
 && test -f /opt/rosetta-plugin/.claude-plugin/plugin.json \
 && test -f /opt/rosetta-plugin/workflows/init-workspace-flow.md \
 && test -d /opt/rosetta-plugin/skills \
 && test -n "$(ls -A /opt/rosetta-plugin/skills)" \
 && echo "✓ Rosetta plugin staged at /opt/rosetta-plugin (from $installed)"

RUN mkdir -p /workspaces
RUN mkdir -p /agent_logs

# Add helper script to initialize ALL heavy mobile SDKs (Android + Flutter template) into the
# persistent volume on demand. Run manually once per volume — one script to remember.
COPY scripts/init-mobile-sdk.sh /usr/local/bin/init-mobile-sdk.sh
RUN chmod +x /usr/local/bin/init-mobile-sdk.sh

# Flutter/Dart PATH entrypoints: each lazily copies the shared template into the per-workspace
# FLUTTER_ROOT on first invocation, then execs the real binary. Installed in /usr/local/bin so
# they shadow any SDK bin on PATH. Keeps non-Flutter workspaces from paying the ~1.5 GB copy and
# gives every workspace an isolated, writable SDK (Flutter can't be shared read-only).
COPY scripts/ensure-flutter-sdk.sh /usr/local/bin/ensure-flutter-sdk
COPY scripts/flutter-wrapper.sh /usr/local/bin/flutter
COPY scripts/dart-wrapper.sh /usr/local/bin/dart
RUN chmod +x /usr/local/bin/ensure-flutter-sdk /usr/local/bin/flutter /usr/local/bin/dart

# Configure git to trust all workspace directories
RUN git config --global --add safe.directory /workspaces

# Keep standards source for copying into workspaces at runtime
RUN mkdir -p /standards_source
COPY app/standards /standards_source/

# Verify Dockerfile build completed successfully
RUN echo "✓ Dockerfile build completed successfully" && \
    echo "✓ Python version: $(python3 --version)" && \
    echo "✓ Uvicorn location: $(test -f .venv/bin/uvicorn && echo 'found' || echo 'missing')" && \
    echo "✓ App directory: $(test -d app && echo 'found' || echo 'missing')" && \
    echo "✓ GitHub CLI: $(gh --version 2>&1 | head -1)" && \
    echo "✓ pnpm: $(pnpm --version 2>&1 | head -1)" && \
    echo "✓ Workspace directories created" && \
    ls -la /workspace* 2>/dev/null | head -3 || echo "Workspace directories ready"

# Expose port for FastAPI (default 8000, Cloud Run will set PORT env var)
EXPOSE 8000/tcp

# Run the application directly from venv
# Use shell form to allow PORT environment variable expansion
# Cloud Run sets PORT automatically, default to 8000 if not set
# Add helpful logging to verify container startup
CMD PORT=${PORT:-8000} && \
    echo "=========================================" && \
    echo "✓ Container starting successfully" && \
    echo "✓ Dockerfile executed correctly" && \
    echo "✓ Starting server on port: $PORT" && \
    echo "✓ Python: $(python3 --version)" && \
    echo "✓ Working directory: $(pwd)" && \
    echo "✓ Uvicorn: $(.venv/bin/uvicorn --version 2>&1 || echo 'checking...')" && \
    echo "=========================================" && \
    exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$PORT"