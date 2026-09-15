FROM python:3.12-slim
# curl: optional QPU_HTTP_CLIENT=curl transport (browser-like TLS
# fingerprint; IBM's Cloudflare edge bans Python's stdlib signature)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server/ ./
EXPOSE 8090
ENV PORT=8090
CMD ["python", "hub.py"]
