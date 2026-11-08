FROM golang:1-alpine3.24 AS builder

RUN apk add --no-cache git ca-certificates build-base su-exec olm-dev

RUN git clone --depth 1 --branch v0.2610.0 https://github.com/mautrix/whatsapp.git /build
COPY status-media-limit.patch /tmp/status-media-limit.patch
WORKDIR /build
RUN git apply /tmp/status-media-limit.patch
RUN ./build.sh

FROM alpine:3.24

ENV UID=1337 \
    GID=1337

RUN apk add --no-cache ffmpeg su-exec ca-certificates olm bash jq curl yq-go lottieconverter

COPY --from=builder /build/mautrix-whatsapp /usr/bin/mautrix-whatsapp
COPY --from=builder /build/docker-run.sh /docker-run.sh
VOLUME /data

CMD ["/docker-run.sh"]
