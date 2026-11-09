FROM eclipse-temurin:25-jre

ARG SIGNAL_CLI_VERSION=0.14.6

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates qrencode \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" | tar -xz -C /opt \
    && ln -s "/opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli" /usr/local/bin/signal-cli

VOLUME /data

CMD ["signal-cli", "--config", "/data", "daemon", "--http=127.0.0.1:18080"]
