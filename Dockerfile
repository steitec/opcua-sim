# opcua-sim in a container: by default the demo plant from examples/plant.yaml (oven,
# pump, pressure transmitter, energy meter) on one OPC UA server, with an endpoint
# without security and one with Basic256Sha256 SignAndEncrypt. Mount your own bench
# file over /bench/bench.yaml.
#
# The endpoint URL says "localhost" (OPCUA_SIM_ENDPOINT_HOST), because many clients
# connect to the URL the server reports. Set it to the name clients use to reach the
# container, e.g. the service name in Docker Compose.
#
# The certificate is kept in /certs (OPCUA_SIM_CERTIFICATE_DIR); mount a directory
# there, writable for UID 10001, and clients have to trust it only once.
#
# The control channel listens on all interfaces inside the container, because
# nothing could reach it otherwise. It has no authentication, so publish it on
# the host's loopback only.
#
#   docker run --rm -p 4840:4840 -p 127.0.0.1:4841:4841 ghcr.io/steitec/opcua-sim
#   docker run --rm -p 4840:4840 -v ./certs:/certs ghcr.io/steitec/opcua-sim
#   docker run --rm -p 4840:4840 -v ./bench.yaml:/bench/bench.yaml ghcr.io/steitec/opcua-sim
FROM python:3.13-slim
LABEL org.opencontainers.image.source=https://github.com/steitec/opcua-sim \
      org.opencontainers.image.description="Simulated OPC UA devices with behaviour, NE 107 health, real alarms and fault injection" \
      org.opencontainers.image.licenses=MIT

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /src

COPY docker/bench.yaml /bench/bench.yaml
RUN useradd --create-home --uid 10001 opcua && mkdir /certs && chown opcua /certs
USER opcua
WORKDIR /bench

ENV OPCUA_SIM_ENDPOINT_HOST=localhost \
    OPCUA_SIM_CERTIFICATE_DIR=/certs
EXPOSE 4840 4841
ENTRYPOINT ["opcua-sim"]
CMD ["serve", "--bench", "/bench/bench.yaml", "--host", "0.0.0.0"]
