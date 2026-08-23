# The agent plane, as one small image.
#
# The harness is standard-library-only by design, so this image needs
# almost nothing: git (the reconciler is a merge queue and does real git
# work) and the GCS client (the run store and lifeblood history). No
# models, no build toolchain, no Cursor CLI - the Cursor transport is
# REST precisely so that nothing has to be installed to talk to it.
#
#   docker build -t turnstile -f cloud/Dockerfile .
#   docker run --rm -e TURNSTILE_AGENT_STORE=gs://your-bucket \
#     -e CURSOR_API_KEY=... -e GH_TOKEN=... turnstile \
#     python -m cloud.tick_all --all --dry-run
#
FROM python:3.11-slim

WORKDIR /app

# git is a runtime dependency here, not a build one: fetch, rebase and
# push are how a merge is performed.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY cloud/requirements.txt /app/cloud/requirements.txt
RUN pip install --no-cache-dir -r cloud/requirements.txt

COPY . /app
RUN pip install --no-cache-dir -e .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
# Where the data repos are checked out. Scratch space for the merge
# queue, deliberately not a persisted volume: every tick resets it.
ENV TURNSTILE_WORKSPACE=/workspace

# Provenance baked at build time, same discipline as the pipeline image:
# a deploy is verified against what is running, never against a tag.
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ENV TURNSTILE_GIT_SHA=$GIT_SHA
ENV TURNSTILE_BUILD_TIME=$BUILD_TIME

CMD ["python", "-m", "cloud.tick_all", "--all"]
