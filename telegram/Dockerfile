FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/puente

EXPOSE 3200
CMD ["uvicorn", "puente.puente:app", "--host", "0.0.0.0", "--port", "3200"]
