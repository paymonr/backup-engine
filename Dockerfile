# syntax=docker/dockerfile:1
FROM alpine:3.20

# Pinned versions — before bumping, fetch the project's official
# SHA256SUMS (restic/rclone) or checksum table (supercronic) for the new
# version/arch, verify the digest against the downloaded asset yourself,
# and update *_SHA256 below. Never bump a version without also updating
# its digest — a stale digest will simply fail the build (which is the
# point), but a version bumped without checking upstream defeats the
# checksum gate entirely.
ARG RESTIC_VERSION=0.17.3
ARG RCLONE_VERSION=1.68.2
ARG SUPERCRONIC_VERSION=0.2.33
ARG TARGETARCH=amd64

# linux/amd64-only for Phase 1 (multi-arch is Phase 4). These digests were
# verified against each project's official published checksums before being
# pinned here — see task-7-report.md for the verification commands/output.
ARG RESTIC_SHA256=5097faeda6aa13167aae6e36efdba636637f8741fed89bbf015678334632d4d3
ARG RCLONE_SHA256=0e6fa18051e67fc600d803a2dcb10ddedb092247fc6eee61be97f64ec080a13c
# supercronic publishes only a SHA1SUM on its release page; we downloaded
# the asset, verified it against that upstream SHA1SUM, then computed this
# SHA256 ourselves from the verified bytes and pinned it here.
ARG SUPERCRONIC_SHA256=feefa310da569c81b99e1027b86b27b51e6ee9ab647747b49099645120cfc671

RUN apk add --no-cache bash ca-certificates curl coreutils tzdata unzip \
      python3 py3-pip aws-cli flock \
    && pip install --no-cache-dir --break-system-packages apprise flask waitress

# restic — download, verify sha256, extract, install.
RUN set -eux; \
    if [ "$TARGETARCH" != "amd64" ]; then echo "unsupported arch: $TARGETARCH (amd64-only in Phase 1)" >&2; exit 1; fi; \
    curl -fsSL -o /tmp/restic.bz2 \
      "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${TARGETARCH}.bz2"; \
    echo "${RESTIC_SHA256}  /tmp/restic.bz2" | sha256sum -c -; \
    bunzip2 /tmp/restic.bz2; \
    install -m0755 /tmp/restic /usr/local/bin/restic; \
    rm -f /tmp/restic; \
    restic version

# rclone — download, verify sha256, extract single binary from the zip, install.
RUN set -eux; \
    if [ "$TARGETARCH" != "amd64" ]; then echo "unsupported arch: $TARGETARCH (amd64-only in Phase 1)" >&2; exit 1; fi; \
    curl -fsSL -o /tmp/rclone.zip \
      "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}.zip"; \
    echo "${RCLONE_SHA256}  /tmp/rclone.zip" | sha256sum -c -; \
    unzip -j /tmp/rclone.zip "*/rclone" -d /usr/local/bin; \
    chmod 0755 /usr/local/bin/rclone; \
    rm -f /tmp/rclone.zip; \
    env -u RCLONE_VERSION rclone version   # the RCLONE_VERSION build arg leaks into env; rclone reads any RCLONE_* var as a flag (here --version), so strip it for the smoke check

# supercronic — download, verify sha256, install.
RUN set -eux; \
    if [ "$TARGETARCH" != "amd64" ]; then echo "unsupported arch: $TARGETARCH (amd64-only in Phase 1)" >&2; exit 1; fi; \
    curl -fsSL -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}"; \
    echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/supercronic; \
    supercronic -version

WORKDIR /app
COPY scripts/ /app/scripts/
COPY app/ /app/app/
COPY config/backup.env.example /app/config/backup.env.example
COPY config/secrets.env.example /app/config/secrets.env.example
RUN chmod +x /app/scripts/*.sh
ENV CACHE_DIR=/cache CONFIG_DIR=/config
VOLUME ["/backup/appdata", "/backup/media", "/config", "/cache"]
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
