# syntax=docker/dockerfile:1
ARG VARIANT=13
FROM debian:${VARIANT} AS base
ARG GREETING_DIR
ARG FROM_CONFIG=no
ENV FIXTURE_ENV="dockerfile" \
    FIXTURE_PATH=${GREETING_DIR}/bin:${PATH}
WORKDIR ${GREETING_DIR}
# Would delete a real account on the host; must be guarded (a real userdel
# of this missing user fails, the guard succeeds).
RUN userdel -r devcontainer-host-nobody && echo guarded > guarded; \
    echo "args: $FROM_CONFIG $VARIANT" > args
COPY files/ copied/
COPY --chmod=755 files/tool.sh bin/tool
RUN <<EOT
set -eu
test "$(pwd)" = "$GREETING_DIR"
echo heredoc > heredoc
EOT
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN [[ -x bin/tool ]] && bin/tool > tool.out
USER vscode
RUN echo "user: $(id -un)" > user
