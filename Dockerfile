FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m appuser && chown -R appuser:appuser /app && chmod +x /app/entrypoint.sh
USER appuser

EXPOSE 5088

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn","--bind","0.0.0.0:5088","--workers","2","--threads","4","--timeout","180","--graceful-timeout","30","--keep-alive","5","--access-logfile","-","--error-logfile","-","app:app"]
