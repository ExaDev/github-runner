FROM catthehacker/ubuntu:act-latest

ARG TARGETPLATFORM=linux/arm64
ENV DEBIAN_FRONTEND=noninteractive
ENV AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gosu jq curl openssl ca-certificates \
        gcc-11 g++-11 \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Bun (JS runtime), installed system-wide
RUN curl -fsSL https://bun.sh/install | BUN_INSTALL=/usr/local bash

RUN groupadd -f docker \
    && useradd -m -s /bin/bash -G docker,sudo runner \
    && mkdir -p /opt/hostedtoolcache /_work /actions-runner

WORKDIR /actions-runner
COPY install_actions.sh .
RUN chmod +x install_actions.sh \
    && GH_RUNNER_VERSION=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r '.tag_name | ltrimstr("v")') \
    && ./install_actions.sh "${GH_RUNNER_VERSION}" "${TARGETPLATFORM}" \
    && rm install_actions.sh \
    && chown -R runner:runner /_work /actions-runner /opt/hostedtoolcache

COPY token.sh entrypoint.sh app_token.sh /
RUN chmod +x /token.sh /entrypoint.sh /app_token.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["./bin/Runner.Listener", "run", "--startuptype", "service"]
