# API image: FastAPI + LangGraph agent.
# Data Flow: docker compose -> build this image -> uvicorn app.main:app
#            -> startup: create tables + seed Postgres, open checkpoint file in /data
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /code

# Dependencies first: this layer is cached until requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as a normal user. It owns the code (eval writes its results there) and /data,
# which holds the agent checkpoints (mounted as a volume).
RUN useradd --create-home app && mkdir -p /data && chown app /data /code
COPY --chown=app . .
USER app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
