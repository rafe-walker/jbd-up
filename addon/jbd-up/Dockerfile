ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}

# The Supervisor builds this with addon/jbd-up/ as the context, so the package is installed
# from GitHub at a pinned ref instead of being copied from the repo root. Bump JBD_UP_REF
# together with config.yaml "version".
ARG JBD_UP_REF=v0.1.0
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN apk add --no-cache git jq \
 && pip install --no-cache-dir "git+https://github.com/rafe-walker/jbd-up@${JBD_UP_REF}" \
 && apk del git

COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD ["/run.sh"]
