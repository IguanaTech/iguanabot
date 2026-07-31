FROM python:3.11-slim

# Sin esto, Python almacena stdout en búfer cuando no hay terminal y los avisos del puente
# ("conectado como @x", "RECHAZADO: contacto ajeno") no aparecen en `docker compose logs` hasta
# que se llena el búfer. Un puente que no se puede mirar en vivo no se puede diagnosticar.
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/puente

EXPOSE 3200
CMD ["uvicorn", "puente.puente:app", "--host", "0.0.0.0", "--port", "3200"]
