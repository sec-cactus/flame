# Flame worker: one container = one `flame run`.
# Build:  docker build -f container/Dockerfile -t flame-worker:latest .
# Auth:   dispatcher mounts host auth dirs (Cursor + OpenCode) read-only.

FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

# Cursor CLI → `agent` on PATH
RUN curl -fsS https://cursor.com/install | bash
ENV PATH="/root/.local/bin:${PATH}"

# OpenCode: fetch via npm, keep only linux-x64 binary, drop Node + other platforms
# (Do not stage under /tmp — BuildKit often mounts it noexec.)
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm \
 && npm install -g opencode-ai --registry=https://registry.npmjs.org \
 && node "$(npm root -g)/opencode-ai/postinstall.mjs" \
 && OC_ROOT="$(npm root -g)/opencode-ai/node_modules" \
 && OC_BIN="$OC_ROOT/opencode-linux-x64/bin/opencode" \
 && test -x "$OC_BIN" \
 && rm -rf "$OC_ROOT"/opencode-linux-x64-* \
 && install -m 755 "$OC_BIN" /opt/opencode \
 && npm uninstall -g opencode-ai \
 && apt-get purge -y nodejs npm \
 && apt-get autoremove -y --purge \
 && rm -rf /var/lib/apt/lists/* /root/.npm /usr/local/lib/node_modules \
 && mv /opt/opencode /usr/local/bin/opencode \
 && /usr/local/bin/opencode --version

# j-space outside ~/.cursor so host mount does not shadow it
RUN git clone --depth 1 \
      https://github.com/Tiger3807861189/J-Space-Cognition-Suite-V3.6 \
      /tmp/jspace-suite \
 && cp -a /tmp/jspace-suite/j-space /opt/j-space \
 && rm -rf /tmp/jspace-suite
ENV FLAME_JSPACE=/opt/j-space

COPY pyproject.toml README.md LICENSE /opt/flame/
COPY src /opt/flame/src
RUN pip install --no-cache-dir /opt/flame \
 && rm -rf /root/.cache/pip

WORKDIR /work
# Dispatcher invokes: flame run --workspace /work --agent-backend …
