ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}

# The add-on reuses the jbd_up package from the repository root (build context = repo root,
# see repository.yaml / the GitHub workflow). For a local build: docker build -f addon/jbd-up/Dockerfile .
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN apk add --no-cache jq

WORKDIR /opt/jbd-up
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY addon/jbd-up/run.sh /run.sh
RUN chmod a+x /run.sh
CMD ["/run.sh"]
