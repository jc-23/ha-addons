# Base image moved here from the (deprecated) build.yaml. The Supervisor still
# overrides BUILD_FROM for multi-arch builds; the default covers a local build.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY max2mqtt.py ./

CMD ["python3", "/app/max2mqtt.py"]
