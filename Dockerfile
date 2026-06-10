FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# Install Google Chrome (the .deb declares and pulls in every shared lib it needs,
# so we don't hand-maintain a fragile package list) plus fonts for decent rendering.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates fonts-liberation \
    && wget -qO- https://dl-ssl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY ["Market Digest/requirements.txt", "./"]
RUN pip install --no-cache-dir -r requirements.txt

# Copy both application directories
COPY ["Market Digest", "./Market Digest/"]
COPY ["SAMC Micro digest", "./SAMC Micro digest/"]

EXPOSE 8000

ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    MARKET_DIGEST_CHROME=/usr/bin/google-chrome

# Serve the portal. Long timeout because report generation runs headless Chrome
# (PDF/PNG) which can take a couple of minutes; single worker keeps the in-process
# generation locks effective, threads allow concurrent lightweight requests.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--chdir", "Market Digest", \
     "--workers", "1", "--threads", "4", "--timeout", "300", \
     "portal_app:app"]
