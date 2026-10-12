FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY greenline_bot ./greenline_bot
# .env и ключ Google монтируются при запуске, в образ не попадают
CMD ["python", "-m", "greenline_bot"]
