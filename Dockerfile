FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 FINIO_ENV=production
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --create-home finio
COPY --chown=finio:finio main.py config.py schemas.py ./
COPY --chown=finio:finio api ./api
COPY --chown=finio:finio modules ./modules
COPY --chown=finio:finio data/sample_transactions.csv data/training_merchants.csv ./data/
COPY --chown=finio:finio data/kb ./data/kb
USER finio
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --no-server-header"]
