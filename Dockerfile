# Phase 10: reproducible deployment
FROM python:3.11.9-slim
ENV PYTHONUNBUFFERED=1 TZ=Asia/Kolkata PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY . .
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:${PORT:-5000}/readyz',timeout=4)" || exit 1
EXPOSE 5000
CMD ["sh","-c","python deployment_check.py --preflight && gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 4 --timeout 900"]
