FROM python:3.12-alpine
LABEL org.opencontainers.image.title="pi-home-dash" \
      org.opencontainers.image.description="Tiny self-hosted home dashboard: app tiles, Immich photo carousel, weather, host and container stats" \
      org.opencontainers.image.licenses="MIT"
RUN pip install --no-cache-dir pyyaml==6.0.2 && adduser -D -H -u 10001 dash
WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4)"
CMD ["python", "-u", "/app/app.py"]
