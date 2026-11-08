FROM node:24-bookworm-slim AS portal
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc \
    npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --all-extras --group build --no-install-project --no-build --no-dev
COPY src/ ./src/
COPY scripts/build_assets.py ./scripts/build_assets.py
COPY README.md LICENSE CHANGELOG.md SECURITY.md ./
COPY --from=portal /build/src/citadel_agent/portal/static ./src/citadel_agent/portal/static
# Runtime assets are generated from the pinned upstream commit before the Docker build.
RUN test -f src/citadel_agent/_assets/manifest.json && \
    uv build --wheel --no-build-isolation --out-dir /wheel && \
    uv export --locked --no-dev --no-emit-project --output-file /runtime-requirements.txt

FROM python:3.11-slim-bookworm AS runtime
ARG BICEP_VERSION=v0.42.1
ARG BICEP_SHA256
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git libicu72 && \
    test -n "$BICEP_SHA256" && \
    curl --fail --location --retry 3 \
      "https://github.com/Azure/bicep/releases/download/${BICEP_VERSION}/bicep-linux-x64" \
      --output /usr/local/bin/bicep && \
    echo "${BICEP_SHA256}  /usr/local/bin/bicep" | sha256sum --check --status && \
    chmod +x /usr/local/bin/bicep && bicep --version && \
    apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY --from=build /runtime-requirements.txt /tmp/runtime-requirements.txt
RUN --mount=type=secret,id=pip_index,env=PIP_INDEX_URL \
    pip install --no-cache-dir --no-compile --require-hashes -r /tmp/runtime-requirements.txt && \
    rm /tmp/runtime-requirements.txt
COPY --from=build /wheel/ /tmp/wheel/
RUN pip install --no-cache-dir --no-compile --no-deps /tmp/wheel/*.whl && \
    rm -rf /tmp/wheel && useradd --create-home --uid 10001 citadel
ENV DOTNET_BUNDLE_EXTRACT_BASE_DIR=/opt/citadel-dotnet
RUN bicep --version && chmod -R a+rX /opt/citadel-dotnet
USER citadel
WORKDIR /home/citadel
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    GIT_TERMINAL_PROMPT=0
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3).close()"]
ENTRYPOINT ["python", "-m", "citadel_agent.portal.server"]
CMD ["agent"]
