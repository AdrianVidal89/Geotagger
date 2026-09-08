FROM python:3.11-slim

# ExifTool es imprescindible: es lo UNICO que escribe metadatos (GPS y fechas).
# Reescribe solo los segmentos de metadatos y copia los pixeles byte a byte,
# asi que las fotos nunca se recomprimen.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libimage-exiftool-perl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY templates/ ./templates/

# Ajustes (carpeta de trabajo, email) y cache de miniaturas. Montar aqui un
# volumen del NAS para que sobrevivan a recrear el contenedor.
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["python", "app.py"]
