FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all Python source files
COPY *.py ./
COPY api/ ./api/

# Create data directory for persistent files (ledger.json, state.json)
RUN mkdir -p /app/data

# Expose the API port
EXPOSE 8000

# Local default 8000; Railway injects PORT — must bind to it for healthchecks/routing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import os,requests; p=os.environ.get('PORT','8000'); requests.get(f'http://127.0.0.1:{p}/api/health', timeout=5)" || exit 1

CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level info"]
