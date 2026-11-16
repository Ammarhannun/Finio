FROM python:3.11-slim@sha256:db20d8506e2f15b6db39b40b6015ca46bae855dec6dd326d462cfe79ae972893
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 FINIO_ENV=production
WORKDIR /app
# Apply available stable-distribution security updates before installing the app.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get -o Acquire::Retries=0 update \
 && apt-get -o Acquire::Retries=0 upgrade -y \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && useradd --create-home finio
# Build tooling is unnecessary in the running service. Remove its bundled
# dependencies too, rather than carrying their vulnerabilities into production.
RUN python -m pip uninstall -y pip setuptools wheel
RUN python -c "import ctypes; ctypes.CDLL('libseccomp.so.2')"
COPY main.py config.py schemas.py ./
COPY api ./api
COPY modules ./modules
COPY data/sample_transactions.csv data/training_merchants.csv ./data/
COPY data/kb ./data/kb
RUN chmod -R go-w /app
USER finio
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --no-server-header --no-access-log"]
