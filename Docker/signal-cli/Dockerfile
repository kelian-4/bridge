FROM eclipse-temurin:25-jre

ARG SIGNAL_CLI_VERSION=0.14.10
ARG SIGNAL_CLI_SHA256=706a57d9faae7525cf732936efdabf210146e3c83f9e8ae7e4e62f5946121c33

RUN set -e; ok=0; \
    for attempt in 1 2 3 4 5; do \
        if apt-get -o Acquire::Retries=5 update \
            && apt-get -o Acquire::Retries=5 install -y --no-install-recommends --fix-missing curl ca-certificates qrencode; then \
            ok=1; break; \
        fi; \
        sleep 5; \
    done; \
    [ "$ok" = 1 ]; \
    rm -rf /var/lib/apt/lists/*

RUN curl -fL --connect-timeout 30 --retry 8 --retry-delay 5 --retry-all-errors -C - \
        -o /tmp/signal-cli.tar.gz \
        "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" \
    && echo "${SIGNAL_CLI_SHA256}  /tmp/signal-cli.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/signal-cli.tar.gz -C /opt \
    && rm /tmp/signal-cli.tar.gz \
    && ln -s "/opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli" /usr/local/bin/signal-cli

VOLUME /data

CMD ["signal-cli", "--config", "/data", "daemon", "--http=127.0.0.1:18080"]
