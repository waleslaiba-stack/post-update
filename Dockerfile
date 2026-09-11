FROM python:3.11-slim-bookworm

# --- Environment ---
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=0 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# --- System deps for Chromium + Playwright ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    wget \
    gnupg \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libxshmfence1 \
    fonts-liberation \
    fonts-noto-color-emoji \
    libx11-xcb1 \
    libxcursor1 \
    libxi6 \
    libxtst6 \
    libgtk-3-0 \
    libgles2 \
    libegl1 \
    && rm -rf /var/lib/apt/lists/*

# --- Python dependencies ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Install Playwright Chromium ---
RUN mkdir -p /ms-playwright && \
    python -m playwright install chromium && \
    chmod -R 755 /ms-playwright

# --- Copy bot source ---
COPY . .

CMD ["python", "bot.py"]
