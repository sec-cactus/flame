# Flame worker: one container = one `flame run`.
# Build:  docker build -f container/Dockerfile -t flame-worker:latest .
# Auth:   dispatcher mounts host ~/.config/cursor:ro (agent login)

FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

# Cursor CLI → `agent` on PATH
RUN curl -fsS https://cursor.com/install | bash
ENV PATH="/root/.local/bin:${PATH}"

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
# Dispatcher invokes: flame run --workspace /work ...
