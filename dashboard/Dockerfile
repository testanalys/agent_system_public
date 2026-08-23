FROM python:3.11-slim

WORKDIR /app

COPY dashboard/requirements.txt /app/dashboard/requirements.txt
RUN pip install --no-cache-dir -r dashboard/requirements.txt

COPY . /app
RUN pip install --no-cache-dir -e .
COPY dashboard/.streamlit /app/.streamlit

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV TURNSTILE_AGENT_STORE=gs://your-bucket
# Streamlit reads config from $HOME/.streamlit or /app/.streamlit when cwd=/app
ENV STREAMLIT_SERVER_PORT=8080
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Cloud Run: containerConcurrency MUST be >>1 (Streamlit loads many JS chunks
# in parallel). Deploy with --concurrency=80 (or default). concurrency=1
# aborts static imports and surfaces as:
#   TypeError: Failed to fetch dynamically imported module

EXPOSE 8080
CMD ["sh", "-c", "streamlit run dashboard/streamlit_app.py --server.port=${PORT:-8080} --server.address=0.0.0.0 --server.headless=true"]
