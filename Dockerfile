# Migration downloader — UI + CLI for the GitHub/GitLab migration APIs.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MD_HOST=0.0.0.0 \
    MD_PORT=8765 \
    DATALABS_ENV_FILE=/app/secrets/.env \
    DATALABS_OUTPUTS_DIR=/data/outputs

WORKDIR /app

# git backs the `wiki` extras
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

# vendor/datalabs_paths.py is the workspace's single definition of the three
# invariants; bootstrap.py prefers the real workspace copy on the host and this
# vendored one inside the image, so the container needs no repo-root mount.
COPY bootstrap.py migration_api.py supplementary.py bitbucket.py for_check.py runner.py jobstore.py redact.py app.py cli.py ./
COPY vendor/ ./vendor/
COPY templates/ ./templates/

RUN useradd --create-home --uid 10001 md \
 && mkdir -p /data/outputs /app/secrets \
 && chown -R md:md /data /app
USER md

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/api/runs',timeout=3)"

CMD ["python", "app.py"]
