# ── unifi-ai-camproxy: ONVIF bridge image (primary, lightweight) ─────────────
#
# This is the new primary image. It does NOT spoof a camera into Protect and
# does NOT run any local YOLO inference. Instead it:
#
#   1. Discovers ONVIF-adopted cameras from your existing Protect controller.
#   2. Subscribes to each camera's native ONVIF event stream (motion, person,
#      vehicle, line-crossing — whatever the camera's onboard AI emits).
#   3. Bridges those events into Protect via bookmarks + Alarm Manager
#      webhooks, so they show up on Protect's timeline and trigger your
#      existing automations.
#
# Why this exists:
#   - The legacy spoof+inference image (Dockerfile.full) is fragile across
#     Protect firmware updates and can't push H.265 natively.
#   - Modern ONVIF cameras already have decent onboard AI; bridging their
#     events is much cheaper than running our own GPU inference.
#   - Final image ~150–200 MB instead of ~2.5 GB.
#
# Status: PREPARATION PHASE — module skeletons are in place, full event
# bridging is the next milestone. See SECONDBRAIN.md "ONVIF bridge mode"
# for the architecture and roadmap.
#
# For the working spoof+inference flow, build / pull `Dockerfile.full`
# (image tag `:full`).

FROM python:3.11-slim

LABEL maintainer="unifi-ai-camproxy"
LABEL description="UniFi Protect ONVIF event bridge (lightweight)"

# Minimal runtime: aiohttp for the web UI + Protect API, onvif-zeep for
# camera event subscription. No ffmpeg, no opencv, no torch, no ultralytics.
WORKDIR /app

COPY requirements-onvif.txt /app/
RUN pip install --no-cache-dir -r /app/requirements-onvif.txt

# Copy the shared modules + the bridge entrypoint.
# unifi_auth and cert_gen are reused from the spoof image's source tree.
COPY src/unifi_auth.py        /app/src/
COPY src/build_info.py        /app/src/
COPY src/onvif_bridge         /app/src/onvif_bridge

# Build metadata — same scheme as Dockerfile.full so the Status tab can
# show which image is actually running.
ARG GIT_SHA=unknown
ARG GIT_REF=unknown
ARG BUILD_TIME=unknown
ENV APP_GIT_SHA=$GIT_SHA
ENV APP_GIT_REF=$GIT_REF
ENV APP_BUILD_TIME=$BUILD_TIME
ENV APP_IMAGE_VARIANT=onvif
RUN printf '%s\n' \
      "git_sha: ${GIT_SHA}" \
      "git_ref: ${GIT_REF}" \
      "build_time: ${BUILD_TIME}" \
      "image_variant: onvif" \
      > /app/BUILD_INFO

VOLUME ["/config"]
EXPOSE 8091

WORKDIR /app/src

# Same env-var entrypoint contract as Dockerfile.full: if /config/config.yml
# exists, use it; if UNIFI_HOST is set, generate one; otherwise crash with
# a helpful message. The bridge's config schema differs from full mode but
# the bootstrap dance is identical.
ENTRYPOINT ["python", "-m", "onvif_bridge.main"]
