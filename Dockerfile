FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PANEL_HOST=0.0.0.0 \
    PANEL_PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY api ./api
COPY bot ./bot
COPY binance_client ./binance_client
COPY database ./database
COPY tools ./tools

RUN useradd --create-home botuser && mkdir -p /app/data /app/logs && chown -R botuser /app
USER botuser

EXPOSE 8000
CMD ["python", "-m", "app.main"]
