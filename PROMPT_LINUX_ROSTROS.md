# Prompt: reconocimiento facial en el equipo Linux

Copia todo lo que hay debajo de la linea en una sesion nueva (Claude Code en el
equipo Linux, dentro de una carpeta vacia o de un repo nuevo).

---

Quiero montar en este equipo Linux un programa de **reconocimiento facial**
para mi biblioteca de fotos. Las fotos viven en un NAS QNAP y las gestiona una
app web mia llamada **Geotagger** (Flask, corre en un contenedor del NAS). El
NAS no tiene potencia para la IA, asi que el analisis lo hace este equipo y
Geotagger solo importa el resultado. Escribelo en **Python 3.11+**, con
comentarios y mensajes en espanol.

## Contexto

- La carpeta de fotos del NAS esta montada por red en este equipo (SMB/NFS),
  por ejemplo en `/mnt/nas/Fotos`. Es la MISMA carpeta que Geotagger tiene como
  "carpeta de trabajo" (en el NAS se llama algo como `/share/Multimedia/Fotos`).
  La ruta de montaje se pasa por parametro; no la des por supuesta.
- Geotagger deja el estado acumulado de rostros en
  `<carpeta de fotos>/.geotagger/faces.json.gz`. Puede no existir (primera vez).
- El flujo es:
  1. Ejecuto tu programa. Lee el estado de `.geotagger/faces.json.gz`, recorre
     la carpeta de fotos y analiza **solo** las fotos nuevas o cambiadas.
  2. Escribe un fichero `faces.json.gz` en el **disco local** de este equipo
     (por defecto `~/geotagger-faces/salida/faces-AAAAMMDD-HHMMSS.json.gz`).
  3. Yo abro Geotagger en el navegador, pulso **Ajustes -> Actualizar
     informacion de rostros**, elijo ese fichero y se sube. Geotagger lo fusiona
     y reescribe `.geotagger/faces.json.gz` con el estado completo (incluidos
     los nombres de persona que yo ponga en Geotagger).

## Reglas que no se pueden romper

- **Nunca escribir, mover, renombrar ni tocar nada en la carpeta de fotos del
  NAS.** Solo lectura. Tampoco escribas en `.geotagger/`: esa carpeta es de
  Geotagger. Abre los archivos en modo lectura y no cambies su fecha de acceso
  si se puede evitar.
- Los ids de persona (`p_XXXX`) tienen que ser **estables entre pasadas**: es
  lo que conecta los nombres que pongo en Geotagger con los clusters. Nunca
  reutilices un id para otra persona ni renumeres.
- Nunca cambies el nombre de una persona que venga con nombre en el estado
  (sobre todo si trae `"name_local": true`, puesto a mano en Geotagger).
- Debe poder interrumpirse (Ctrl+C, apagado) y retomarse sin repetir trabajo.

## Formato de intercambio `geotagger-faces` v1 (obligatorio)

Un unico JSON en UTF-8, comprimido con gzip:

```json
{
  "format": "geotagger-faces",
  "version": 1,
  "mode": "incremental",
  "generated_at": "2026-10-11T10:00:00Z",
  "generator": {"name": "geotagger-faces-linux", "version": "0.1", "model": "buffalo_l"},
  "root": "/mnt/nas/Fotos",
  "embedding": {"dim": 512, "dtype": "float32", "metric": "cosine"},
  "persons": [
    {"id": "p_0001", "name": "Ana", "cover": "3f2a9c1e0b7d4a55"},
    {"id": "p_0002", "name": null}
  ],
  "photos": [
    {
      "path": "2024/Viaje/IMG_0001.jpg",
      "size": 4831220, "mtime": 1717171717,
      "width": 4000, "height": 3000,
      "analyzed_at": "2026-10-11T09:58:12Z",
      "faces": [
        {"id": "3f2a9c1e0b7d4a55", "box": [0.412, 0.188, 0.091, 0.122],
         "score": 0.98, "person": "p_0001",
         "embedding": "<base64 de 512 float32 little-endian>"}
      ]
    },
    {"path": "2024/Viaje/IMG_0002.jpg", "size": 3911002, "mtime": 1717171720,
     "width": 4000, "height": 3000, "faces": []}
  ]
}
```

- `path`: relativa a la carpeta de fotos, con `/`, sin `/` inicial.
- `size` y `mtime` (segundos, entero) del archivo cuando se analizo. Una foto
  esta "cambiada" si difiere el tamano o el mtime en mas de 2 s (SMB/NFS
  redondean).
- `faces: []` para las fotos sin caras: tambien hay que listarlas, asi constan
  como analizadas y no se repiten.
- `box`: `[x, y, w, h]` normalizados 0..1 sobre la imagen **ya orientada**
  (aplica la orientacion EXIF antes de detectar), origen arriba a la izquierda.
- `faces[].id`: unico global, p. ej. `sha1(path + "#" + box redondeado)[:16]`.
- `embedding`: base64 de `dim` valores `float32` little-endian, normalizados
  L2. Los embeddings de las caras ya conocidas vienen en el estado de
  `.geotagger/faces.json.gz`: **no recalcules las caras viejas**, usalos.
- `mode`:
  - `"incremental"` (lo normal): el fichero lleva solo las fotos analizadas en
    esta pasada **mas** las fotos viejas cuyas caras cambiaron de persona
    (reagrupadas), y la lista completa de `persons`.
  - `"full"` (opcion `--full`): todas las fotos de la carpeta. Geotagger borra
    del indice de rostros lo que no venga.
- Si una foto del estado ya no existe en la carpeta, no la incluyas; con
  `--full` desaparecera de Geotagger.

## Que fotos analizar

- Extensiones: `.jpg .jpeg .tif .tiff .png .webp .heic .heif` y RAW
  `.cr3 .cr2 .nef .arw .raf .dng .jpr`.
- Salta carpetas y archivos que empiecen por `.` o `@` (`@Recycle`,
  `.@__thumb`, `.geotagger`... son del QNAP o de Geotagger).
- RAW: no los reveles; usa la vista previa JPEG incrustada (`exiftool -b
  -PreviewImage` / `-JpgFromRaw`, o `rawpy` si hace falta). HEIC con
  `pillow-heif`. Orientacion con `PIL.ImageOps.exif_transpose`.
- Para detectar basta con reducir la imagen a ~1600-2000 px de lado mayor; las
  coordenadas se guardan normalizadas, asi que valen para el original.

## Modelo y agrupacion

- Deteccion + embeddings: **InsightFace** (`buffalo_l`, ArcFace 512-d) sobre
  `onnxruntime-gpu` si hay GPU NVIDIA y `onnxruntime` en CPU si no. Detecta la
  GPU automaticamente y dilo al arrancar. Descarta caras con `score` < 0.6 o
  con lado menor de ~40 px en la imagen reducida (configurable).
- Agrupacion incremental:
  1. Centroides de las personas existentes a partir de los embeddings del
     estado.
  2. Cada cara nueva se asigna a la persona mas cercana si la similitud coseno
     supera un umbral (por defecto ~0.45, configurable y calibrable).
  3. Las que no se asignan se agrupan entre si (HDBSCAN o DBSCAN con metrica
     coseno, `min_samples` pequeno). Cada grupo con al menos N caras (por
     defecto 3) es una persona nueva con el siguiente id libre `p_XXXX`; el
     resto quedan con `person: null` y se reintentan en la proxima pasada.
  4. No fusiones ni partas personas que ya tienen nombre. Si dos personas SIN
     nombre resultan ser la misma, puedes fusionarlas conservando el id menor
     (e incluye en el fichero las fotos afectadas).
- `cover` de cada persona: la cara con mejor `score` y tamano.

## Rendimiento y robustez

- Leer por red es el cuello de botella: lee archivos en paralelo (varios hilos
  de E/S) mientras el modelo procesa por lotes en el hilo principal.
- Cache local en SQLite (`~/geotagger-faces/cache.db`) con lo analizado
  (`path`, `size`, `mtime`, caras, embeddings) para retomar tras una
  interrupcion y no depender solo del estado del NAS. Al arrancar, el estado de
  `.geotagger/faces.json.gz` manda sobre la cache (puede traer fotos movidas
  desde Geotagger, nombres nuevos, etc.).
- Una foto que no se puede abrir se apunta en un log y se sigue; no aborta.
- Barra de progreso (`tqdm`) con fotos/s y estimacion de tiempo.

## Interfaz

CLI `geotagger-faces` (o `python -m geotagger_faces`) con:

- `--photos /mnt/nas/Fotos` (obligatorio).
- `--out DIR` carpeta de salida local.
- `--full` pasada completa; `--limit N` para probar con pocas fotos;
  `--only SUBCARPETA` para analizar solo una parte.
- `--threshold`, `--min-face`, `--min-cluster` para afinar.
- `--dry-run`: dice cuantas fotos nuevas/cambiadas hay y no analiza.
- Al terminar imprime un resumen (fotos analizadas, caras, personas nuevas,
  caras sin asignar) y la ruta del fichero a subir a Geotagger.

## Entregables

1. Paquete Python con `pyproject.toml`, `README.md` en espanol (instalacion con
   `venv`, GPU opcional, como montar el NAS con `cifs` o `nfs` en `/etc/fstab`
   en solo lectura, ejemplo de uso y de timer de `systemd` para lanzarlo de
   noche).
2. Validador del formato: una funcion/comando `validate FICHERO` que
   compruebe que un `faces.json.gz` cumple la especificacion de arriba.
3. Tests con `pytest`: lectura del estado, deteccion de fotos nuevas/cambiadas
   (incluido el margen de 2 s de mtime), ids de persona estables entre dos
   pasadas, que nunca se escribe en la carpeta de fotos (usa una carpeta de
   prueba en solo lectura) y que la salida pasa el validador.

Antes de escribir codigo, comprueba que version de Python, GPU/CUDA y espacio
en disco hay en este equipo y dime que vas a instalar.
