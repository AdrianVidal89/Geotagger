# ── Revelado en el NAS ───────────────────────────────────────────────────────
# El motor del editor (el mismo que corre en el navegador como WebAssembly),
# compilado para el procesador del NAS: guardar una edicion se revela aqui y
# no en el movil (ver /api/edit_develop en app.py). Se compila una vez; Docker
# reutiliza esta capa mientras no cambie lightcraft-bridge/. Si la compilacion
# fallara, la imagen se crea igual y el editor revela en el navegador como antes.
FROM rust:1-slim-bookworm AS lightcraft
WORKDIR /src
COPY lightcraft-bridge/ ./
# LTO "thin": misma velocidad medida que "fat" y bastante menos memoria y
# tiempo al compilar en la CPU del NAS
RUN mkdir -p /out \
 && (CARGO_PROFILE_RELEASE_LTO=thin CARGO_PROFILE_RELEASE_CODEGEN_UNITS=8 \
     cargo build --release --locked --features cli --bin lc-export \
     && cp target/release/lc-export /out/) \
 || echo "AVISO: lc-export no se pudo compilar; el editor revelara en el navegador" \
 ; rm -rf target

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
# Editor LightCraft: motor ya compilado a WebAssembly (no hace falta Rust aqui)
COPY static/ ./static/
# ...y su version para el NAS, si se pudo compilar (carpeta vacia si no)
COPY --from=lightcraft /out/ /usr/local/bin/

# Ajustes (carpeta de trabajo, email) y cache de miniaturas. Montar aqui un
# volumen del NAS para que sobrevivan a recrear el contenedor.
ENV PYTHONUNBUFFERED=1
# Abrir una foto grande reserva y suelta decenas de MB de golpe. Por defecto la
# libreria de C guarda una reserva por hilo (y Flask crea un hilo por peticion),
# asi que esa memoria no vuelve al sistema y el contenedor se queda "ocupando"
# cientos de MB que ya no usa. Con dos reservas compartidas, la memoria se
# devuelve: medido en el NAS, 129 MB -> 51 MB despues de llenar la galeria.
ENV MALLOC_ARENA_MAX=2

EXPOSE 5000

CMD ["python", "app.py"]
