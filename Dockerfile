FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn

COPY codebuddy_direct_api.py .
COPY server.py .

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "python -m uvicorn server:app --host 0.0.0.0 --port ${PORT} --log-level info"]
