# ClipFlow: automated AI highlight clipping pipeline with web dashboard and ffmpeg
# Based on PeakCut (MIT License). Maintained by Mirza8975.
FROM python:3.11-slim

LABEL org.opencontainers.image.title="ClipFlow" \
      org.opencontainers.image.description="AI highlight generator and vertical clip maker" \
      org.opencontainers.image.source="https://github.com/Mirza8975/ClipFlow" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates bzip2 fonts-noto-core fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*
# yt-dlp needs a JavaScript runtime for YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY studio studio
COPY web web

ENV CLIPFLOW_DATA=/data \
    PEAKCUT_DATA=/data \
    CLIPFLOW_HOST=0.0.0.0 \
    PEAKCUT_HOST=0.0.0.0 \
    CLIPFLOW_PORT=8765 \
    PEAKCUT_PORT=8765 \
    CLIPFLOW_TRUST_PROXY=1 \
    PEAKCUT_TRUST_PROXY=1 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"
# the first start downloads the speech model into /data/models by itself (about 480 MB)
CMD ["python", "-m", "studio.server"]
