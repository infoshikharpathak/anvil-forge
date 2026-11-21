# anvil-forge control plane. Build from the repo root:
#   docker build -f server/Dockerfile -t anvil-forge-server .
#   docker run -p 8200:8200 -v anvil-forge-data:/data anvil-forge-server
# Postgres instead of the bundled SQLite file: -e DATABASE_URL=postgresql+psycopg://user:pass@host/db
FROM python:3.12-slim

# git: the SDK depends on anvil-ai and agent-forge as git dependencies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 forge \
    && mkdir /data && chown forge /data

WORKDIR /srv
COPY sdk/ sdk/
COPY server/ server/
RUN pip install --no-cache-dir ./sdk "./server[postgres]"

USER forge
ENV DATABASE_URL=sqlite:////data/anvil_forge_server.db \
    ANVIL_FORGE_TRUST_PROXY=1
VOLUME /data
EXPOSE 8200
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8200/health')"
CMD ["anvil-forge-server", "serve", "--host", "0.0.0.0", "--port", "8200"]
