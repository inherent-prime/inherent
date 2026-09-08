# Local S3 stand-in for the dev/CI stack.
#
# Why this image exists (#353)
# ----------------------------
# The compose service used to be a bare `node:20-alpine` running
# `npx s3rver ...`, which downloads the package from npm on EVERY container
# start. Health probes begin the moment the container starts, so that install
# raced the healthcheck budget: `docker compose up --wait` tore the whole
# stack down mid-download and failed the required E2E smoke gate on four
# unrelated branches in one morning.
#
# Installing at BUILD time removes the race rather than widening it: compose
# finishes the build before it starts a container, so no probe can fire while
# npm is still working. A slow registry now makes the build slow instead of
# making the lane red.
FROM node:20-alpine

# Pinned, not floating: a bare `npx s3rver` re-resolved whatever npm served
# that day, so the S3 implementation under every integration and E2E run could
# change with no commit (same class of drift as #225).
ARG S3RVER_VERSION=3.7.1
RUN npm install -g --no-fund --no-audit "s3rver@${S3RVER_VERSION}"

# `wget` for the healthcheck is already in the busybox userland of alpine.
EXPOSE 9000
ENTRYPOINT ["s3rver"]
