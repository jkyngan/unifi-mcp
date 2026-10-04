FROM python@sha256:3dd7cc108ec1493442514f5c2a871af6af0ec31d768ff6e378a93340c3b3db5f AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 SOURCE_DATE_EPOCH=1791129600
WORKDIR /source
COPY recipe/build-requirements.txt recipe/runtime-requirements.txt /recipe/
COPY inputs /inputs
RUN python -m pip install --no-index --find-links=/inputs/build --only-binary=:all: --require-hashes --no-deps -r /recipe/build-requirements.txt && python -m pip check
RUN python -m pip download --no-index --find-links=/inputs/runtime --only-binary=:all: --require-hashes --no-deps -r /recipe/runtime-requirements.txt --dest /wheels
RUN python -c "import hashlib; from pathlib import Path; assert hashlib.sha256(Path('/inputs/antlr.tar.gz').read_bytes()).hexdigest() == 'f224469b4168294902bb1efa80a8bf7855f24c99aef99cbefc1bcd3cce77881b'" \
 && python -m pip wheel --no-index --no-build-isolation --no-deps /inputs/antlr.tar.gz --wheel-dir /wheels
COPY source/packages/unifi-core packages/unifi-core
COPY source/packages/unifi-mcp-shared packages/unifi-mcp-shared
COPY source/apps/protect apps/protect
RUN SETUPTOOLS_SCM_PRETEND_VERSION=0.4.56.dev6+g7fa82e05 python -m pip wheel --no-index --no-build-isolation --no-deps ./packages/unifi-core --wheel-dir /wheels \
 && SETUPTOOLS_SCM_PRETEND_VERSION=0.6.21.dev20+g7fa82e05 python -m pip wheel --no-index --no-build-isolation --no-deps ./packages/unifi-mcp-shared --wheel-dir /wheels \
 && SETUPTOOLS_SCM_PRETEND_VERSION=0.8.11.dev4+g7fa82e05 python -m pip wheel --no-index --no-build-isolation --no-deps ./apps/protect --wheel-dir /wheels
FROM python@sha256:3dd7cc108ec1493442514f5c2a871af6af0ec31d768ff6e378a93340c3b3db5f
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
 python -m venv /app/.venv && /app/.venv/bin/python -m pip install --no-index --no-deps /wheels/*.whl && /app/.venv/bin/python -m pip check
COPY source/LICENSE /app/LICENSE
LABEL org.opencontainers.image.source="https://github.com/jkyngan/unifi-mcp" \
 org.opencontainers.image.revision="7fa82e05040ddf1c8a6d1916ce4f399417e8e25d" \
 org.opencontainers.image.licenses="MIT" \
 org.opencontainers.image.version="0.8.11.dev4+g7fa82e05"
EXPOSE 3001
CMD ["unifi-protect-mcp"]
