# Agent image for server-autoops: Node + DeepSeek Harness (dsh, MIT, installed from npm).
# Build with plain `docker build` (not Compose) so the image carries no Compose project labels:
#   docker build -t local/server-autoops-agent:0.1.0 agent-image/
# The ops plugin is NOT baked in; the runner bind-mounts plugin/ read-only at run time.
ARG NODE_IMAGE=node:24-bookworm-slim
FROM ${NODE_IMAGE}
ARG DSH_VERSION=0.2.1-rc.5
RUN npm install --prefix /opt/dsh --omit=dev --no-audit --no-fund \
        "@deepseek-ai/dsh@${DSH_VERSION}" "@deepseek-ai/dsh-llm-pi-ai@${DSH_VERSION}" \
    && npm cache clean --force \
    && mkdir -p /opt/dsh/node_modules/@local/autoops /workspace /run/autoops \
    && chown node:node /workspace
ENV PATH="/opt/dsh/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    HOME=/home/node \
    DSH_HOME=/home/node/.dsh \
    DSH_TELEMETRY_DISABLED=1
LABEL org.opencontainers.image.title="server-autoops-agent" \
      org.opencontainers.image.description="Headless dsh with only the server-autoops ops tool (tool set is enforced by the runner profile)" \
      org.opencontainers.image.licenses="MIT" \
      io.github.rmqg.server-autoops.image="agent" \
      com.docker.compose.project="" com.docker.compose.service="" com.docker.compose.version=""
USER node
WORKDIR /workspace
CMD ["dsh", "--version"]
