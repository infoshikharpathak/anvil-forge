# bastion control plane. Build from the repo root:
#   docker build -f server/Dockerfile -t bastion-server .
#   docker run -p 8200:8200 -v bastion-data:/data bastion-server
# Postgres instead of the bundled SQLite file: -e DATABASE_URL=postgresql+psycopg://user:pass@host/db
FROM python:3.12-slim

# git: the SDK depends on anvil-ai and agent-forge as git dependencies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 bastion \
    && mkdir /data && chown bastion /data

WORKDIR /srv
COPY sdk/ sdk/
COPY server/ server/
RUN pip install --no-cache-dir ./sdk "./server[postgres]"

USER bastion
ENV DATABASE_URL=sqlite:////data/bastion_server.db \
    BASTION_TRUST_PROXY=1
VOLUME /data
EXPOSE 8200
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8200/health')"
CMD ["bastion-server", "serve", "--host", "0.0.0.0", "--port", "8200"]
