# media-tools, self-contained: every binary it calls ships in the image.
# Design: docs/specs/2026-09-25-media-tools-container-design.md
#
# Targets:
#   prod   what a production host runs. `download` and `ebook kindle` are disabled by a
#          file baked onto the read-only root, which no environment variable can undo.
#   local  the same image with nothing disabled, for running it on a workstation.
#   test   local plus the dev dependencies and the test suite. CI runs it; nothing
#          deploys it.
#
# Base: Ubuntu 24.04, pinned by digest. Calibre 7.6 and Python 3.12 from its archive
# are the combination the full test suite already passes on (vps-remote-desktop).
# Calibre comes from the archive, never from upstream's installer script.

ARG BASE=ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3

# -- build: a venv holding media-tools and its hash-locked dependencies ---------------
FROM ${BASE} AS build
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/media-tools
ENV PATH=/opt/media-tools/bin:$PATH \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock /src/requirements.lock
# --require-hashes fails the build if any requirement, indirect ones included, is left
# unpinned: the lock cannot quietly become incomplete.
RUN pip install --require-hashes --no-deps -r /src/requirements.lock
COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
# The project itself has no hash (it is built here); --no-deps keeps pip from
# resolving anything the lock did not already install.
RUN pip install --no-deps /src

# -- runtime: what every target shares -----------------------------------------------
FROM ${BASE} AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 calibre ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# uid/gid 10001: never 1000, which is the deploy user on the production host.
RUN groupadd --gid 10001 media && useradd --uid 10001 --gid 10001 --no-create-home media
COPY --from=build /opt/media-tools /opt/media-tools
ENV PATH=/opt/media-tools/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp/home \
    MEDIA_TOOLS_OUT=/data/out \
    QT_QPA_PLATFORM=offscreen
# A named volume mounted at /data is initialised with this directory's owner, so it
# must belong to the runtime user, or nothing can be written there.
RUN mkdir -p /data/out && chown -R 10001:10001 /data
WORKDIR /data
ENTRYPOINT ["media-tools"]
CMD ["--help"]

# -- local: nothing disabled -----------------------------------------------------------
FROM runtime AS local
USER 10001:10001

# -- test: the offline suite, run as the runtime user ---------------------------------
FROM runtime AS test
COPY requirements-dev.lock /src/requirements-dev.lock
RUN /opt/media-tools/bin/pip install --no-cache-dir --require-hashes --no-deps \
        -r /src/requirements-dev.lock
COPY --chown=10001:10001 pyproject.toml /src/pyproject.toml
COPY --chown=10001:10001 tests /src/tests
COPY --chown=10001:10001 README.md CLAUDE.md /src/
# test_doctor checks the session-start hook's venv instruction, so the suite reads it.
COPY --chown=10001:10001 .claude/settings.json /src/.claude/settings.json
WORKDIR /src
USER 10001:10001
ENTRYPOINT ["python3", "-m", "pytest", "-m", "not network and not llm and not device", "-p", "no:cacheprovider"]
CMD ["-q"]

# -- prod: download and ebook kindle refused, by the image itself ----------------------
FROM runtime AS prod
RUN mkdir -p /usr/local/share/media-tools \
    && printf 'download\nebook-kindle\n' > /usr/local/share/media-tools/disabled-tasks \
    && chmod 0444 /usr/local/share/media-tools/disabled-tasks
USER 10001:10001
# Production runs the job API; any other command is still `docker run ... <task>`.
CMD ["serve"]
