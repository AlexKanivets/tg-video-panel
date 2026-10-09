FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg — для сжатия видео; отдельный пользователь — панель не работает от root
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin tvp

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && (pip install cryptg || echo "cryptg не установился — не критично")

COPY app.py docker-entrypoint.sh ./
RUN chmod 644 app.py requirements.txt && chmod 755 docker-entrypoint.sh && mkdir -p /data /videos

ENV TVP_SERVER=1 TVP_HOST=0.0.0.0 TVP_PORT=8765 TVP_DATA_DIR=/data TVP_DOWNLOAD_DIR=/videos
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/favicon.svg', timeout=4)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "app.py"]
