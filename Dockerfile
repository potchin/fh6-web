FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

COPY server.py .
COPY static/ static/

EXPOSE 8080
EXPOSE 20440/udp

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
