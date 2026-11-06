FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config WORK_DIR=/work PORT=8080

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY loxbridge ./loxbridge
RUN pip install --no-cache-dir . \
 && groupadd -g 568 apps && useradd -u 568 -g 568 -M -d /config -s /usr/sbin/nologin apps

# Starts as root only to create /config, /work, /backups and hand them to PUID:PGID (default 568 = TrueNAS
# "apps"), then drops to that user before anything else runs (loxbridge/cli.py drop_privileges).
ENV PUID=568 PGID=568
VOLUME ["/config", "/backups", "/work"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s%s/api/v1/health' % (os.environ.get('PORT','8080'), os.environ.get('BASE_URL','')), timeout=5)"
ENTRYPOINT ["python", "-m", "loxbridge"]
CMD ["serve"]
