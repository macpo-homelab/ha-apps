ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.21
FROM ${BUILD_FROM}

# Der Quellcode wird von scripts/build_addon.py nach ./app kopiert (Build-Kontext = dieser Ordner).
WORKDIR /opt/device-manager
COPY app/ ./
RUN pip3 install --no-cache-dir . \
    && rm -rf /root/.cache

ENV DM_OPTIONS_FILE=/data/options.json \
    PYTHONUNBUFFERED=1

CMD ["python3", "-m", "device_manager"]
