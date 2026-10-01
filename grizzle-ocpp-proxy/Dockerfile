# Home Assistant passes BUILD_FROM when building locally; the default covers standalone builds.
ARG BUILD_FROM=python:3.13-alpine
FROM ${BUILD_FROM}

ARG BUILD_VERSION=dev
LABEL io.hass.name="Grizzl-E OCPP Proxy" \
      io.hass.description="Repairs the OCPP frames a Grizzl-E EV charger gets wrong" \
      io.hass.type="addon" \
      io.hass.version="${BUILD_VERSION}" \
      org.opencontainers.image.source="https://github.com/kccricket/grizzle-ocpp-proxy" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore --disable-pip-version-check -r requirements.txt

COPY proxy.py .

EXPOSE 8321
CMD ["python", "proxy.py"]
