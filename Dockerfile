FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/ ./server/
ENV DATA_DIR=/data BEHIND_PROXY=1 PORT=8080
WORKDIR /app/server
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8080", "app:app"]
