FROM python:3.12-slim
WORKDIR /app
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api/main.py .
COPY index.html .
COPY favicon.svg .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8087"]
