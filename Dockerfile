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
# Abrir una foto grande reserva y suelta decenas de MB de golpe. Por defecto la
# libreria de C guarda una reserva por hilo (y Flask crea un hilo por peticion),
# asi que esa memoria no vuelve al sistema y el contenedor se queda "ocupando"
# cientos de MB que ya no usa. Con dos reservas compartidas, la memoria se
# devuelve: medido en el NAS, 129 MB -> 51 MB despues de llenar la galeria.
ENV MALLOC_ARENA_MAX=2

# Sello de version. Lo pasa el script de actualizacion
# (--build-arg GIT_COMMIT=...) y la app lo publica en /api/version, que es la
# unica forma FIABLE de saber que el NAS esta corriendo lo que crees.
# Van al final a proposito: cambiar el sello no invalida la cache de las capas
# de arriba, asi que reconstruir sigue costando segundos y no minutos.
ARG GIT_COMMIT=desconocido
ARG GIT_BRANCH=desconocido
ARG BUILD_DATE=desconocido
ENV GEOTAGGER_COMMIT=$GIT_COMMIT \
    GEOTAGGER_BRANCH=$GIT_BRANCH \
    GEOTAGGER_BUILD=$BUILD_DATE

EXPOSE 5000

# Container Station enseña el estado en la lista de contenedores, y el script
# de actualizacion lo espera antes de dar el despliegue por bueno.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5000/api/version',timeout=4)" || exit 1

CMD ["python", "app.py"]
