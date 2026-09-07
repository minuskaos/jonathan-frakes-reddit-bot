FROM python:3.13-slim
LABEL org.opencontainers.image.source="https://github.com/minuskaos/jonathan-frakes-reddit-bot"
LABEL org.opencontainers.image.description="Jonathan Frakes Reddit watcher bot with an Unraid-friendly web dashboard"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py default-config.json ./
RUN mkdir -p /data
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3)"
CMD ["python", "-u", "/app/app.py"]
