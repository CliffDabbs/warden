# Warden — single-container parental-control hub.
# Based on the official Playwright/Python image so the headless scrapers (Atom, Weduc)
# have Chromium + all system libs preinstalled. Pin matches playwright==1.61.0.
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

# (if the -noble tag is unavailable for this playwright version, use v1.61.0-jammy)

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WARDEN_HOST=0.0.0.0 \
    WARDEN_PORT=8080 \
    WARDEN_DB=/app/data/warden.db

WORKDIR /app

# Deps first for layer caching. Plain uvicorn + wsproto = no C build tools needed.
COPY requirements.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt

# App (package + web assets + default config)
COPY warden ./warden
COPY web ./web
COPY config ./config

# Persist the SQLite state (rules, signals, audit) across restarts.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8080

# python -m warden reads WARDEN_* + AdGuard/Anthropic creds from the environment.
CMD ["python", "-m", "warden"]
