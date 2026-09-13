import os
import math
import subprocess
import re
import io
import time
import json
import ctypes
import base64
import hashlib
import tempfile
import shutil
import sqlite3
import threading
import zipfile
import smtplib
from contextlib import contextmanager
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from flask import Flask, jsonify, request, render_template, send_file
from PIL import Image, ImageOps, ImageChops, ImageFilter
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# Raiz del NAS visible DENTRO del contenedor: todo lo que la app puede
# recorrer cuelga de aqui. Se puede fijar con la variable de entorno NAS_ROOT;
# si no, se usa el primer punto de montaje que exista.
# IMPORTANTE: para poder navegar por mas carpetas del NAS hay que montarlas en
# el contenedor (p. ej. -v /share:/nas) y apuntar NAS_ROOT a ese montaje.
def _detect_nas_root():
    env = os.environ.get("NAS_ROOT", "").strip()
    if env:
        return env
    for candidate in ("/nas", "/share", "/photos"):
        if os.path.isdir(candidate):
            return candidate
    return "/photos"

NAS_ROOT = _detect_nas_root()
# Carpeta de trabajo inicial (relativa a NAS_ROOT) mientras no se elija otra
DEFAULT_WORK_ROOT = os.environ.get("WORK_ROOT", "").strip().strip("/")

SETTINGS_DIR = "/app/data"
SETTINGS_FILE = SETTINGS_DIR + "/settings.json"
THUMB_CACHE_DIR = SETTINGS_DIR + "/thumb_cache"
# Los archivos grandes que la app arma al vuelo (el ZIP de una descarga) se
# montan AQUI, no en memoria: un ZIP de veinte fotos son cientos de MB que el
# contenedor tendria retenidos hasta que el navegador termine de bajarlo.
TMP_DIR = SETTINGS_DIR + "/tmp"
os.makedirs(SETTINGS_DIR, exist_ok=True)
os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

JPG_EXTS = {".jpg", ".jpeg", ".tiff", ".tif"}
PNG_EXTS = {".png"}
RAW_EXTS = {".cr3", ".jpr", ".cr2", ".nef", ".arw", ".raf", ".dng"}
WEBP_EXTS = {".webp"}
HEIC_EXTS = {".heic", ".heif"}

# pillow-heif registra un DECODIFICADOR HEIC/HEIF en Pillow (fotos de
# iPhone/iPad). Solo afecta a la LECTURA para miniaturas y previews; la
# escritura de GPS sigue siendo exclusiva de ExifTool, que soporta HEIC.
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_SUPPORT = True
except Exception:
    HEIF_SUPPORT = False

# Conjunto de todo lo que la app muestra y sabe geoetiquetar. PNG incluido:
# ExifTool escribe GPS en PNG (chunk eXIf) y Pillow genera su miniatura.
# WebP tambien: ExifTool escribe EXIF/GPS en WebP extendido. HEIC solo se
# muestra si pillow-heif esta instalado (sin el no habria miniatura/preview).
SUPPORTED_EXTS = JPG_EXTS | PNG_EXTS | RAW_EXTS | WEBP_EXTS
if HEIF_SUPPORT:
    SUPPORTED_EXTS = SUPPORTED_EXTS | HEIC_EXTS
# Extensiones permitidas al SUBIR fotos. Incluye las que ya soporta la app
# mas los formatos habituales de la galeria de iPhone/iPad (HEIC/HEIF).
UPLOAD_EXTS = SUPPORTED_EXTS | HEIC_EXTS | WEBP_EXTS

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.environ.get("SMTP_USER", "ecostruxureatlas@gmail.com")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

# =============================================================================
# PRINCIPIO FUNDAMENTAL DE ESTA APP:
# NUNCA se recomprimen ni se re-codifican las imagenes originales.
# Toda escritura de metadatos (GPS, etc.) se hace EXCLUSIVAMENTE con ExifTool,
# que reescribe solo los segmentos de metadatos y copia los datos de pixel
# byte a byte, sin tocar la calidad de la imagen.
# Pillow se usa UNICAMENTE para LEER y generar miniaturas en memoria (buffers),
# NUNCA para guardar/sobrescribir archivos originales.
# El renombrado es solo una operacion de sistema de archivos (rename), que no
# altera el contenido del archivo en absoluto.
# =============================================================================

def _safe_join(rel, base):
    """Safely resolve a user-supplied relative path under base.
    Returns None if the resolved path escapes the base directory."""
    base_p = Path(base).resolve()
    try:
        target = (base_p / (rel or "")).resolve()
    except Exception:
        return None
    if not str(target).startswith(str(base_p) + os.sep) and str(target) != str(base_p):
        return None
    return target

def _work_root():
    """Carpeta de trabajo actual (absoluta). Es la raiz que ve la galeria.
    Si la guardada ya no existe (montaje caido, carpeta borrada) se vuelve a
    la raiz del NAS para no dejar la app sin nada que mostrar."""
    rel = _load_settings().get("work_root", DEFAULT_WORK_ROOT)
    target = _safe_join(rel, NAS_ROOT)
    if target is None or not target.is_dir():
        return Path(NAS_ROOT).resolve()
    return target

def _work_root_rel():
    """Carpeta de trabajo relativa a la raiz del NAS ("" = la propia raiz)."""
    root = _work_root()
    base = Path(NAS_ROOT).resolve()
    return "" if root == base else str(root.relative_to(base))

def _work_root_info():
    rel = _work_root_rel()
    return {
        "path": rel,
        "name": rel.split("/")[-1] if rel else (Path(NAS_ROOT).name or "NAS"),
        "abs": str(_work_root()),
        "nas_root": NAS_ROOT,
        "nas_name": Path(NAS_ROOT).name or "NAS",
    }

def _favorites():
    """Accesos directos a carpetas, guardados como rutas relativas a la raiz del
    NAS para que sigan valiendo aunque se cambie la carpeta de trabajo."""
    favs = _load_settings().get("favorites", [])
    out = []
    for f in favs if isinstance(favs, list) else []:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path", "")).strip().strip("/")
        name = str(f.get("name", "")).strip() or (path.split("/")[-1] if path else "NAS")
        out.append({"path": path, "name": name})
    return out

def _resolve_path(rel, base=None):
    """Resuelve una ruta relativa DENTRO de la carpeta de trabajo actual
    (o de la base indicada). Devuelve None si se sale de ella."""
    return _safe_join(rel, base if base is not None else _work_root())

# Los ajustes se leen en casi todas las peticiones (cada miniatura resuelve su
# ruta), asi que se cachean y solo se releen cuando cambia el fichero.
_settings_cache = {"mtime": None, "data": None}

def _load_settings():
    defaults = {"recipient_email": "", "work_root": DEFAULT_WORK_ROOT}
    try:
        mtime = os.path.getmtime(SETTINGS_FILE)
    except OSError:
        return dict(defaults)
    if _settings_cache["mtime"] != mtime:
        try:
            with open(SETTINGS_FILE, "r") as f:
                _settings_cache["data"] = json.load(f)
            _settings_cache["mtime"] = mtime
        except Exception:
            return dict(defaults)
    data = dict(defaults)
    data.update(_settings_cache["data"] or {})
    return data

def _save_settings(data):
    """Guarda mezclando con lo que ya habia: asi guardar el email no borra la
    carpeta de trabajo (y al reves)."""
    current = _load_settings()
    current.update(data or {})
    with open(SETTINGS_FILE, "w") as f:
        json.dump(current, f)
    _settings_cache["mtime"] = None

def _send_report(subject, body_html):
    settings = _load_settings()
    recipient = settings.get("recipient_email", "").strip()
    if not recipient or not SMTP_PASS:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = recipient
        msg.attach(MIMEText(body_html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, recipient, msg.as_string())
    except Exception as e:
        print("Email error:", str(e))

def sanitize_path(path):
    p = Path(path)
    clean_name = re.sub(r'[:\*\?"<>\|]', '-', p.name)
    if clean_name != p.name:
        new_path = p.parent / clean_name
        p.rename(new_path)
        return new_path
    return p

def _trigger_reindex(path):
    p = Path(path)
    tmp = p.parent / ("." + p.name + ".reindex_tmp")
    try:
        p.rename(tmp)
        tmp.rename(p)
    except Exception:
        if tmp.exists():
            try:
                tmp.rename(p)
            except Exception:
                pass

def _write_gps_exiftool(path, lat, lon, alt=None):
    """
    Escribe coordenadas GPS usando ExifTool con -overwrite_original.
    ExifTool reescribe SOLO los metadatos, copiando los datos de imagen
    byte a byte. NO recomprime ni reduce la calidad.
    """
    path = sanitize_path(path)
    lat_ref = "N" if lat >= 0 else "S"
    lon_ref = "E" if lon >= 0 else "W"
    cmd = [
        "exiftool",
        "-GPSLatitude=" + str(abs(lat)),
        "-GPSLatitudeRef=" + lat_ref,
        "-GPSLongitude=" + str(abs(lon)),
        "-GPSLongitudeRef=" + lon_ref,
        "-overwrite_original",
    ]
    if alt is not None:
        cmd.append("-GPSAltitude=" + str(abs(alt)))
        cmd.append("-GPSAltitudeRef=" + ("0" if alt >= 0 else "1"))
    cmd.append(str(path))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(result.stderr.strip())
    _trigger_reindex(path)
    # Invalidate thumbnail cache for this file
    _evict_thumb_cache(path)
    _index_set_gps(path, lat, lon)

def _cache_key(abs_path):
    return hashlib.md5(str(abs_path).encode()).hexdigest()

def _cache_dir_for(key):
    """Las miniaturas van repartidas en 256 subcarpetas por las dos primeras
    letras del hash: con miles de fotos, buscar en una sola carpeta obligaba a
    recorrerla entera en cada operacion."""
    return Path(THUMB_CACHE_DIR) / key[:2]

EXIFTOOL_CHUNK = 200      # fotos por llamada, para no pasarse de linea de comandos

def _chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]

def _after_write(path):
    """Lo que toca hacer tras modificar una foto: reindexar en el NAS, tirar sus
    miniaturas y marcarla para releer en el indice."""
    _trigger_reindex(path)
    _evict_thumb_cache(path)

def _exiftool_updated(stdout):
    """Cuantos archivos dice ExifTool haber actualizado. Ojo con el limite de
    palabra: "20 image files updated" contiene "0 image files updated"."""
    m = re.search(r"(?:^|\D)(\d+) image files? updated", stdout or "")
    return int(m.group(1)) if m else 0

def _exiftool_updated_none(stdout):
    return _exiftool_updated(stdout) == 0

# Una fecha de metadatos tiene que parecer una fecha. Algunos campos EXIF
# guardan el texto con un prefijo de codificacion ("ASCII\0\0\0..."), y un
# archivo con los metadatos danados puede devolver cualquier cosa donde deberia
# ir la fecha. Antes que ensenar un disparate -o guardarlo en el indice- es
# mejor no saberla y leer el archivo.
DATE_SHAPE = re.compile(r"^\d{4}[:-]\d{2}[:-]\d{2}[ T]\d{2}:\d{2}:\d{2}")

def _clean_date(raw):
    """Devuelve la fecha si tiene forma de fecha; si no, cadena vacia."""
    if not raw:
        return ""
    txt = str(raw).strip()
    if not DATE_SHAPE.match(txt) or txt.startswith("0000"):
        return ""
    return txt

def _ui_date(raw):
    """De "2005:06:15 10:00:00" (ExifTool) a "2005-06-15 10:00:00"."""
    txt = _clean_date(raw)
    if not txt:
        return ""
    return re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", txt)

def _read_dates_batch(paths):
    """Fechas de un lote con UNA sola llamada. Devuelve por foto la fecha de
    toma y la primera que haya de las tres que toca -AllDates: si no hay
    ninguna, un desplazamiento no cambiaria nada."""
    out = {}
    if not paths:
        return out
    cmd = ["exiftool", "-fast2", "-json", "-q", "-q", "-SourceFile",
           "-DateTimeOriginal", "-CreateDate", "-ModifyDate"] + [str(p) for p in paths]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=max(120, 5 * len(paths)))
        for row in json.loads(result.stdout or "[]"):
            origen = row.get("SourceFile")
            if not origen:
                continue
            toma = _clean_date(row.get("DateTimeOriginal"))
            out[os.path.realpath(origen)] = {
                "toma": toma,
                "alguna": toma or _clean_date(row.get("CreateDate"))
                          or _clean_date(row.get("ModifyDate")),
            }
    except Exception:
        pass
    return out

# Escribir metadatos en un PNG grande no es cuestion de disco: ExifTool recorre
# el archivo entero en Perl (unos 10 MB/s), asi que un PNG de 18 MB cuesta 1,8 s
# frente a 0,3 s de un JPEG de 30 MB. Es tiempo de CPU, y eso si se reparte:
# los mismos 8 PNG pasan de 13,1 s a 3,5 s en 4 procesos.
EXIFTOOL_WORKERS = min(4, os.cpu_count() or 1)
# Repartir solo cuando compensa: con fotos normales una sola llamada ya va en
# decimas y no vale la pena arrancar mas procesos.
EXIFTOOL_PARALLEL_BYTES = 24 * 1024 * 1024

class _Lote:
    """Resultado de escribir un lote, venga de uno o de varios procesos."""
    def __init__(self, returncode=0, updated=0, stderr=""):
        self.returncode, self.updated, self.stderr = returncode, updated, stderr

def _exiftool_write(args, paths):
    cmd = ["exiftool"] + args + ["-overwrite_original"] + [str(p) for p in paths]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=max(120, 20 * len(paths)))
    return _Lote(r.returncode, _exiftool_updated(r.stdout), (r.stderr or "").strip())

def _total_bytes(paths):
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(str(p))
        except OSError:
            pass
    return total

def _run_exiftool_batch(args, paths):
    """Aplica los MISMOS cambios a varias fotos. Lanzar un proceso por foto era
    lo que hacia lentos los lotes; con archivos grandes, al reves, un solo
    proceso deja los demas nucleos parados."""
    paths = list(paths)
    trozos = [paths]
    if len(paths) > 1 and EXIFTOOL_WORKERS > 1 and \
       _total_bytes(paths) >= EXIFTOOL_PARALLEL_BYTES:
        n = min(EXIFTOOL_WORKERS, len(paths))
        trozos = [t for t in (paths[i::n] for i in range(n)) if t]
    if len(trozos) == 1:
        return _exiftool_write(args, paths)
    with ThreadPoolExecutor(max_workers=len(trozos)) as ex:
        partes = list(ex.map(lambda t: _exiftool_write(args, t), trozos))
    return _Lote(max(p.returncode for p in partes),
                 sum(p.updated for p in partes),
                 "\n".join(p.stderr for p in partes if p.stderr))

def _evict_thumb_cache(abs_path):
    """Borra las miniaturas y vistas previas guardadas de una foto.
    Antes comparaba md5(ruta) contra nombres que son md5(ruta+fecha+formato):
    no coincidian nunca, asi que no borraba nada y encima recorria toda la
    carpeta de cache en cada llamada."""
    try:
        key = _cache_key(abs_path)
        for f in _cache_dir_for(key).glob(key[:16] + "_*"):
            f.unlink(missing_ok=True)
    except Exception:
        pass

# =============================================================================
# LECTURA DE IMAGENES: MEMORIA Y CONCURRENCIA
# Descomprimir una foto grande es con diferencia lo mas caro que hace la app:
# un JPEG de 48 Mpx (unos 25-40 MB en disco) ocupa 144 MB por copia en memoria
# y tarda casi un segundo solo en abrirse. Nada de lo que la app ENSENA -la
# miniatura, la vista previa de 2048 px, el histograma de 512 px- necesita esa
# resolucion, asi que aqui estan las cuatro medidas que mantienen el consumo a
# raya. Todas juntas son la diferencia entre 384 MB por foto abierta y 99 MB:
#   1. draft(): el decodificador JPEG entrega la imagen YA reducida (escalas
#      1/2, 1/4 u 1/8 del DCT, que salen casi gratis). Hay que pedirselo antes
#      de tocar los pixeles: en cuanto algo los lee, ya se descomprimieron
#      todos. Por eso ORIENTAR VA AL FINAL y no al principio.
#   2. reduce() en vez de copy() cuando solo hace falta una version pequena.
#   3. un tope de decodificaciones a la vez: Flask atiende cada peticion en su
#      propio hilo y la galeria pide muchas fotos de golpe; sin tope, N fotos
#      grandes a la vez multiplican por N la memoria del contenedor.
#   4. malloc_trim(): sin el, lo que Pillow libera se queda en el proceso (el
#      asignador de C no lo devuelve al sistema) y el contenedor se queda
#      ocupando cientos de MB que ya no usa hasta que se reinicia.
# =============================================================================

# Cuantas fotos se pueden estar descomprimiendo a la vez: el techo de memoria
# de la app pasa a ser algo conocido, y no el numero de pestanas abiertas.
# Una por nucleo, medido en el NAS de 4 (12 miniaturas de 40 MB / 8 vistas
# previas a la vez / pico de memoria):
#     2 turnos -> 2,27 s / 3,17 s / 214 MB
#     3 turnos -> 1,67 s / 2,52 s / 290 MB
#     4 turnos -> 1,18 s / 1,67 s / 383 MB   <-- uno por nucleo
#     6 turnos -> 1,32 s / 2,08 s / 360 MB   (repartir de mas sale peor)
# Dejar un nucleo libre "para que la interfaz responda" parecia buena idea y
# resulto ser solo mas lento: a quien hay que apartar del camino no es a las
# peticiones del usuario, sino al trabajo de fondo, y de eso se encarga
# _usuario_quieto(). El tope de 4 es el que mantiene la memoria acotada.
DECODE_SLOTS = max(2, min(4, os.cpu_count() or 2))
_decode_sem = threading.BoundedSemaphore(DECODE_SLOTS)

try:
    _libc = ctypes.CDLL("libc.so.6")
except Exception:
    _libc = None

def _release_memory():
    """Devuelve al sistema la memoria que Pillow ya solto."""
    if _libc is None:
        return
    try:
        _libc.malloc_trim(0)
    except Exception:
        pass

@contextmanager
def _decode_slot():
    """Turno para descomprimir una foto. Al salir devuelve la memoria."""
    with _decode_sem:
        try:
            yield
        finally:
            _release_memory()

# Peticiones del usuario que estan esperando turno o descomprimiendo. El
# pre-generado de fondo mira este numero para apartarse: el que esta mirando la
# pantalla va primero, siempre.
_user_decodes = 0
_user_last = 0.0
_user_guard = threading.Lock()

@contextmanager
def _user_decode():
    """Turno de decodificacion para una peticion del usuario."""
    global _user_decodes, _user_last
    with _user_guard:
        _user_decodes += 1
    try:
        with _decode_slot():
            yield
    finally:
        with _user_guard:
            _user_decodes -= 1
            _user_last = time.time()

def _usuario_quieto(margen):
    """Cierto si el usuario no tiene nada en marcha y lleva `margen` segundos
    sin pedir nada. No basta con mirar si hay algo AHORA MISMO: entre dos
    miniaturas de la misma pantalla hay milisegundos de hueco, y colarse ahi
    significa quedarse con un turno durante los 0,35 s que tarda la siguiente
    foto, que es justo lo que el usuario esta esperando."""
    return _user_decodes == 0 and (time.time() - _user_last) > margen

# Una sola generacion por archivo de cache: si dos peticiones piden a la vez la
# misma vista previa (el visor y la miniatura, o dos pestanas), la segunda
# espera a la primera y se lleva la cache ya hecha en lugar de descomprimir la
# foto por segunda vez.
_render_locks = {}
_render_guard = threading.Lock()

@contextmanager
def _render_once(key):
    with _render_guard:
        entry = _render_locks.get(key)
        if entry is None:
            entry = [threading.Lock(), 0]
            _render_locks[key] = entry
        entry[1] += 1
    entry[0].acquire()
    try:
        yield
    finally:
        entry[0].release()
        with _render_guard:
            entry[1] -= 1
            if entry[1] <= 0:
                _render_locks.pop(key, None)

def _open_scaled(abs_path, box):
    """Abre una foto pidiendo al decodificador que no se moleste en entregar
    mas resolucion de la que cabe en `box`. Es LECTURA: no toca el original."""
    img = Image.open(str(abs_path))
    _draft(img, box)
    return img

def _draft(img, box):
    """Pide la escala reducida al decodificador. En los formatos que no la
    soportan (PNG, WebP) no hace nada, y no pasa nada."""
    try:
        img.draft(None, box)
    except Exception:
        pass

def _fit(img, box):
    """Ajusta al recuadro y ORIENTA DESPUES. Girar una foto de 48 Mpx cuesta
    otra copia entera (144 MB) y ademas obliga a descomprimirla del todo;
    girar la ya reducida no cuesta nada y da exactamente el mismo resultado,
    porque el recuadro es cuadrado."""
    img.thumbnail(box)
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img

def _downscaled(img, box):
    """Copia reducida de una imagen YA abierta. reduce() promedia por bloques y
    devuelve directamente algo pequeno; copy()+thumbnail(), que es lo que habia
    antes, reservaba los 48 Mpx enteros para tirarlos acto seguido."""
    n = max(1, min(img.width // max(1, box[0]), img.height // max(1, box[1])))
    small = img.reduce(n) if n > 1 else img.copy()
    small.thumbnail(box)
    return small

# Orientaciones EXIF en las que la foto se ve girada un cuarto de vuelta, o sea
# con el ancho y el alto intercambiados respecto a lo que dice el archivo.
SWAP_ORIENTATIONS = {5, 6, 7, 8}

def _oriented_size(img):
    """Tamano tal como se VE, leyendo solo la cabecera (sin descomprimir)."""
    w, h = img.size
    try:
        orient = img.getexif().get(0x0112, 1)
    except Exception:
        orient = 1
    return (h, w) if orient in SWAP_ORIENTATIONS else (w, h)

def _cache_store(cache_file, data):
    """Guarda un archivo de cache de una sola vez. Escribir directamente sobre
    el destino deja una ventana en la que otra peticion puede encontrarselo a
    medio escribir y servir una imagen rota."""
    tmp = cache_file.with_name(cache_file.name + "." + str(threading.get_ident()) + ".part")
    try:
        tmp.write_bytes(data)
        os.replace(str(tmp), str(cache_file))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise

# =============================================================================
# MINIATURAS POR ADELANTADO
# Abrir una carpeta POR PRIMERA VEZ es lo unico que seguia costando: cada
# miniatura obliga a descomprimir la foto, y en un JPEG de 40 MB son 0,35 s que
# no hay forma de bajar. No es cuestion de resolucion -pedir 1/8 no es mas
# rapido que pedir 1/4, porque el grueso es descomprimir los 40 MB de datos-,
# asi que sesenta fotos de esas son ocho segundos mirando huecos grises.
#
# Lo que si se puede es que no espere nadie: al listar una carpeta, un hilo de
# fondo va generando las miniaturas que falten EN EL MISMO ORDEN en que se ven.
# Cuando el usuario baja, ya estan hechas (de 134 ms a 1,8 ms por foto).
# Reglas para no estorbar en un NAS que comparte sitio con otros contenedores:
#   - un solo hilo, y solo de la carpeta que se esta mirando;
#   - se aparta mientras haya peticiones del usuario en marcha;
#   - se para en seco en cuanto se abre otra carpeta;
#   - se rinde tras un tope de fotos: lo que el usuario ve al bajar un rato.
# =============================================================================

PREFETCH_MAX = 600          # fotos por carpeta
PREFETCH_PAUSA = 0.1        # cada cuanto se vuelve a mirar si el usuario paro
# Margen de silencio antes de ponerse a trabajar. Tiene que ser mayor que lo
# que tarda una foto (0,35 s), o el fondo se cuela entre dos miniaturas de la
# pantalla que el usuario esta mirando y se la retrasa.
PREFETCH_MARGEN = 0.5
# Al abrir una carpeta, el navegador tarda un momento en pedir las miniaturas
# que se ven. Sin esta espera, el fondo llega antes y le quita el sitio.
PREFETCH_ARRANQUE = 0.6
# Siempre UN turno menos de los que hay: asi, por mucho que el fondo trabaje,
# al usuario nunca le toca esperar a que se libere una plaza.
PREFETCH_WORKERS = max(1, DECODE_SLOTS - 1)

_prefetch_gen = 0
_prefetch_guard = threading.Lock()

def _prefetch_request(folder, names):
    """Arranca el pre-generado de una carpeta y cancela el de la anterior."""
    global _prefetch_gen
    if not names:
        return
    with _prefetch_guard:
        _prefetch_gen += 1
        gen = _prefetch_gen
    names = names[:PREFETCH_MAX]
    # Repartidas de forma intercalada: entre todos los hilos, la lista avanza
    # en orden, que es como se van a ir viendo al bajar.
    for i in range(min(PREFETCH_WORKERS, len(names))):
        threading.Thread(target=_prefetch_worker,
                         args=(Path(folder), names[i::PREFETCH_WORKERS], gen),
                         daemon=True).start()

def _prefetch_vigente(gen):
    return _prefetch_gen == gen

def _prefetch_worker(folder, names, gen):
    hechas = 0
    time.sleep(PREFETCH_ARRANQUE)
    for name in names:
        if not _prefetch_vigente(gen):
            return
        # El usuario primero: no se toca nada hasta que lleve un rato quieto
        while not _usuario_quieto(PREFETCH_MARGEN):
            if not _prefetch_vigente(gen):
                return
            time.sleep(PREFETCH_PAUSA)
        path = folder / name
        # WEBP es lo que pide cualquier navegador desde 2020, y es la unica
        # version que vale la pena adelantar: adelantar las dos seria el doble
        # de trabajo para que sobre la mitad.
        cache_file = _thumb_cache_path(path, "WEBP")
        if cache_file.exists():
            continue
        try:
            with _render_once(str(cache_file)):
                if not cache_file.exists():
                    with _decode_slot():
                        _make_thumb(path, cache_file, "WEBP")
                    hechas += 1
        except Exception:
            pass            # una foto rota no puede parar el resto
    _blur_flush()
    if hechas and _prefetch_vigente(gen):
        _cache_sweep()

# =============================================================================
# TOPE DE LA CACHE EN DISCO
# Las miniaturas son pequenas (unos 8 KB) pero las vistas previas no (de 50 KB
# a casi 1 MB cada una), y hasta ahora nada las borraba nunca: solo se tiraban
# las de una foto cuando esa foto cambiaba. Una biblioteca grande vista foto a
# foto puede dejar varios GB en el volumen del NAS creciendo para siempre.
# Al pasarse del tope se tiran las MENOS USADAS hasta bajar al 85%; son
# ficheros regenerables, no se pierde nada.
# =============================================================================

def _cache_limite_bytes():
    try:
        mb = int(os.environ.get("THUMB_CACHE_MAX_MB", "1024"))
    except ValueError:
        mb = 1024
    return max(0, mb) * 1024 * 1024      # 0 = sin tope

CACHE_SWEEP_GAP = 1800      # segundos entre repasos, como poco
_cache_sweep_at = 0
# Recorrer la cache entera son miles de stat(): que no lo hagan dos hilos a la
# vez (el pre-generado lanza varios y el indice tambien lo pide al terminar).
_cache_sweep_lock = threading.Lock()

def _cache_sweep():
    """Recorta la cache si se paso del tope. Solo mira tamanos y fechas."""
    global _cache_sweep_at
    limite = _cache_limite_bytes()
    if limite <= 0:
        return
    if time.time() - _cache_sweep_at < CACHE_SWEEP_GAP:
        return
    if not _cache_sweep_lock.acquire(blocking=False):
        return
    try:
        _cache_sweep_at = time.time()
        ficheros, total = [], 0
        for dirpath, _, names in os.walk(THUMB_CACHE_DIR):
            for n in names:
                f = os.path.join(dirpath, n)
                try:
                    st = os.stat(f)
                except OSError:
                    continue
                total += st.st_size
                # atime donde el sistema lo actualiza, mtime donde no: para una
                # cache las dos fechas sirven para saber que sobra antes
                ficheros.append((max(st.st_atime, st.st_mtime), st.st_size, f))
        if total <= limite:
            return
        objetivo = limite * 0.85
        ficheros.sort()                  # las mas antiguas primero
        for _, size, f in ficheros:
            if total <= objetivo:
                break
            try:
                os.unlink(f)
                total -= size
            except OSError:
                pass
        print("Cache de miniaturas recortada a %.0f MB" % (total / 1024 / 1024))
    except Exception as e:
        print("Cache sweep error:", str(e))
    finally:
        _cache_sweep_lock.release()

def _has_gps_fast(path):
    """Lee (sin modificar) si el archivo ya tiene coordenadas GPS."""
    try:
        result = subprocess.run(
            ["exiftool", "-n", "-GPSLatitude", "-GPSLongitude", "-s", "-s", "-s", str(path)],
            capture_output=True, text=True, timeout=10
        )
        out = result.stdout.strip()
        return bool(out) and len(out.split("\n")) >= 2
    except Exception:
        return False

def _get_coords(path):
    try:
        result = subprocess.run(
            ["exiftool", "-n", "-GPSLatitude", "-GPSLongitude", "-s", "-s", "-s", str(path)],
            capture_output=True, text=True, timeout=10
        )
        out = result.stdout.strip()
        if out and "\n" in out:
            parts = out.split("\n")
            return float(parts[0].strip()), float(parts[1].strip())
    except Exception:
        pass
    return None

# =============================================================================
# FECHAS DE METADATOS
# Igual que con el GPS, la escritura de fechas se hace SOLO con ExifTool, que
# reescribe los segmentos de metadatos y copia los pixeles byte a byte.
# -AllDates cubre DateTimeOriginal, CreateDate y ModifyDate de una vez.
# =============================================================================

EXIF_DATE_FMT = "%Y:%m:%d %H:%M:%S"
# Formato con el que se devuelven las fechas a la interfaz
UI_DATE_FMT = "%Y-%m-%d %H:%M:%S"

def _parse_user_datetime(value):
    """Convierte la fecha que manda la interfaz (input datetime-local,
    'AAAA-MM-DDTHH:MM[:SS]') al formato de ExifTool 'AAAA:MM:DD HH:MM:SS'.
    Devuelve None si la fecha no es valida."""
    v = (value or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y:%m:%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).strftime(EXIF_DATE_FMT)
        except ValueError:
            continue
    return None

def _build_shift(shift):
    """Construye el desplazamiento de ExifTool a partir de
    {sign, days, hours, minutes}. Devuelve (operador, valor) o None.
    El valor tiene el formato de shift de ExifTool: 'A:M:D h:m:s'."""
    if not isinstance(shift, dict):
        return None
    try:
        days = abs(int(shift.get("days") or 0))
        hours = abs(int(shift.get("hours") or 0))
        minutes = abs(int(shift.get("minutes") or 0))
    except (TypeError, ValueError):
        return None
    if days == 0 and hours == 0 and minutes == 0:
        return None
    op = "-=" if str(shift.get("sign", "+")) == "-" else "+="
    return op, "0:0:" + str(days) + " " + str(hours) + ":" + str(minutes) + ":0"

def _read_dates(path):
    """Lee (sin modificar) las fechas del archivo: la de la foto (EXIF) y la
    del propio fichero. Devuelve un dict con las que existan."""
    out = {}
    try:
        result = subprocess.run(
            ["exiftool", "-json", "-d", UI_DATE_FMT,
             "-DateTimeOriginal", "-CreateDate", "-ModifyDate", "-FileModifyDate",
             str(path)],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(result.stdout)[0]
    except Exception:
        return out
    pairs = (("date", "DateTimeOriginal"), ("create_date", "CreateDate"),
             ("modify_date", "ModifyDate"), ("file_date", "FileModifyDate"))
    for key, tag in pairs:
        val = data.get(tag)
        if isinstance(val, str) and DATE_SHAPE.match(val.strip()) and not val.startswith("0000"):
            out[key] = val.strip()
    return out

def _write_date_exiftool(path, exif_date=None, shift=None, sync_file=True):
    """
    Escribe la fecha de los metadatos con ExifTool y -overwrite_original.
    - exif_date: fecha absoluta ya en formato 'AAAA:MM:DD HH:MM:SS'.
    - shift: tupla (operador, valor) para desplazar las fechas existentes.
    - sync_file: ademas de los metadatos, ajusta la fecha del archivo para que
      la galeria (que ordena por fecha de archivo) muestre lo mismo.
    NO recomprime ni re-codifica la imagen.
    """
    path = sanitize_path(path)
    cmd = ["exiftool"]
    if shift:
        op, val = shift
        cmd.append("-AllDates" + op + val)
        if sync_file:
            cmd.append("-FileModifyDate" + op + val)
    else:
        cmd.append("-AllDates=" + exif_date)
        if sync_file:
            cmd.append("-FileModifyDate=" + exif_date)
    cmd += ["-overwrite_original", str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise Exception(result.stderr.strip() or "error de ExifTool")
    # ExifTool devuelve 0 aunque no escriba nada (p.ej. shift sin fecha previa)
    if _exiftool_updated_none(result.stdout):
        if shift:
            raise Exception("la foto no tiene fecha previa que desplazar")
        raise Exception(result.stderr.strip().splitlines()[0] if result.stderr.strip()
                        else "no se pudo escribir la fecha")
    _trigger_reindex(path)
    _evict_thumb_cache(path)
    _index_stale(path)
    return path

# =============================================================================
# EDICION DE IMAGEN
# Girar/voltear es SIN PERDIDA: solo se reescribe el tag EXIF Orientation, los
# pixeles no se tocan (funciona hasta en RAW).
# Recortar y ajustar luz/color obligan a recodificar los pixeles; la interfaz
# avisa antes de guardar. Los metadatos (GPS, fechas) se copian del original
# con ExifTool para no perderlos.
# =============================================================================

# Composicion de operaciones sobre el tag Orientation (1-8). Tablas verificadas
# contra las transposiciones de Pillow, no de memoria.
ORIENT_OPS = {
    "cw":    {1: 6, 2: 7, 3: 8, 4: 5, 5: 2, 6: 3, 7: 4, 8: 1},
    "ccw":   {1: 8, 2: 5, 3: 6, 4: 7, 5: 4, 6: 1, 7: 2, 8: 3},
    "180":   {1: 3, 2: 4, 3: 1, 4: 2, 5: 7, 6: 8, 7: 5, 8: 6},
    "fliph": {1: 2, 2: 1, 3: 4, 4: 3, 5: 6, 6: 5, 7: 8, 8: 7},
    "flipv": {1: 4, 2: 3, 3: 2, 4: 1, 5: 8, 6: 7, 7: 6, 8: 5},
}

# Formatos cuyos pixeles se pueden reescribir. Un RAW no: al editarlo se
# guarda un JPEG derivado junto al original.
EDITABLE_EXTS = JPG_EXTS | PNG_EXTS | WEBP_EXTS | HEIC_EXTS

def _rotate_lossless(path, op):
    """Gira/voltea cambiando SOLO el tag EXIF Orientation. No toca los pixeles."""
    path = sanitize_path(path)
    table = ORIENT_OPS.get(op)
    if table is None:
        raise Exception("operacion no valida")
    current = _read_orientation(path)
    if current not in table:
        current = 1
    new = table[current]
    result = subprocess.run(
        ["exiftool", "-Orientation=" + str(new), "-n", "-overwrite_original", str(path)],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0 or _exiftool_updated_none(result.stdout):
        raise Exception(result.stderr.strip().splitlines()[0] if result.stderr.strip()
                        else "no se pudo girar")
    _trigger_reindex(path)
    _evict_thumb_cache(path)
    _index_stale(path)
    return new

# ── Ajustes de luz y color ───────────────────────────────────────────────────
# El editor del navegador previsualiza con filtros CSS y un multiply en canvas.
# Aqui se replica la MISMA formula, en el mismo orden, para que lo que se
# guarda sea exactamente lo que se veia:
#   1. temperatura: atenua el azul (calido) o el rojo (frio)
#   2. brillo:      v * b
#   3. contraste:   (v - 0.5) * c + 0.5
#   4. saturacion:  matriz de CSS saturate() con luma Rec.709
LUMA_R, LUMA_G, LUMA_B = 0.213, 0.715, 0.072

# Orden de la cadena (identico en el editor del navegador):
#   1. niveles (punto negro / punto blanco)
#   2. temperatura y tinte (ganancia por canal)
#   3. sombras y luces (curvas gamma)
#   4. brillo
#   5. contraste
#   6. saturacion (matriz, fuera del LUT porque mezcla canales)
def _channel_gains(temp, tint):
    t = max(-100.0, min(100.0, float(temp or 0))) / 400.0
    n = max(-100.0, min(100.0, float(tint or 0))) / 400.0
    return 1.0 + t, 1.0 - n, 1.0 - t

def _wb_ranges(adj):
    """Rangos por canal del equilibrio de color: [[loR,hiR],[loG,hiG],[loB,hiB]]
    o None. Estirar cada canal por separado es lo que quita la dominante
    amarillenta o magenta de una foto vieja, algo que un estirado comun no
    puede hacer (aplasta el canal que vive en la franja mas baja)."""
    wb = adj.get("wb")
    if not wb:
        return None
    try:
        out = []
        for key in ("r", "g", "b"):
            lo, hi = wb[key]
            lo = max(0.0, min(254.0, float(lo)))
            hi = max(lo + 1.0, min(255.0, float(hi)))
            out.append((lo, hi))
        return out
    except (KeyError, TypeError, ValueError, IndexError):
        return None

def _tone_lut(adj):
    """Tabla de 768 valores (256 por canal) con toda la cadena tonal."""
    wb = _wb_ranges(adj)
    black = max(0.0, min(240.0, _adj_value(adj, "black", 0.0)))
    white = max(black + 1.0, min(255.0, _adj_value(adj, "white", 255.0)))
    shadows = max(-100.0, min(100.0, _adj_value(adj, "shadows", 0.0)))
    highlights = max(-100.0, min(100.0, _adj_value(adj, "highlights", 0.0)))
    brightness = _adj_value(adj, "brightness", 1.0)
    contrast = _adj_value(adj, "contrast", 1.0)
    gains = _channel_gains(_adj_value(adj, "temp", 0.0), _adj_value(adj, "tint", 0.0))
    p_sh = 1.0 / (1.0 + 0.9 * shadows / 100.0)
    p_hi = 1.0 + 0.9 * highlights / 100.0

    lut = []
    for ch, gain in enumerate(gains):
        for v in range(256):
            x = float(v)
            if wb:
                lo, hi = wb[ch]
                x = 255.0 * (x - lo) / (hi - lo)
                x = max(0.0, min(255.0, x))
            x = (x - black) / (white - black)
            x = max(0.0, min(1.0, x)) * gain
            x = max(0.0, min(1.0, x))
            if shadows:
                x = x ** p_sh
            if highlights:
                x = 1.0 - (1.0 - x) ** p_hi
            x = x * brightness
            x = (x - 0.5) * contrast + 0.5
            lut.append(max(0, min(255, int(round(x * 255.0)))))
    return lut

def _saturation_matrix(s):
    s = float(s)
    return (
        LUMA_R + (1 - LUMA_R) * s, LUMA_G - LUMA_G * s,       LUMA_B - LUMA_B * s,       0,
        LUMA_R - LUMA_R * s,       LUMA_G + (1 - LUMA_G) * s, LUMA_B - LUMA_B * s,       0,
        LUMA_R - LUMA_R * s,       LUMA_G - LUMA_G * s,       LUMA_B + (1 - LUMA_B) * s, 0,
    )

def _adj_value(adj, key, default):
    try:
        return float(adj.get(key, default))
    except (TypeError, ValueError):
        return default

# Valores neutros: si el ajuste esta en su valor por defecto no se toca nada
ADJ_NEUTRAL = {"black": 0.0, "white": 255.0, "shadows": 0.0, "highlights": 0.0,
               "temp": 0.0, "tint": 0.0, "brightness": 1.0, "contrast": 1.0,
               "saturation": 1.0}

def _adj_is_neutral(adj):
    if _wb_ranges(adj):
        return False
    return all(_adj_value(adj, k, v) == v for k, v in ADJ_NEUTRAL.items())

def _apply_adjustments(img, adj):
    if _adj_is_neutral(adj):
        return img
    saturation = _adj_value(adj, "saturation", 1.0)
    tonal = any(_adj_value(adj, k, v) != v
                for k, v in ADJ_NEUTRAL.items() if k != "saturation")
    alpha = None
    if img.mode == "RGBA":
        alpha = img.getchannel("A")
    if img.mode != "RGB":
        img = img.convert("RGB")
    if tonal or _wb_ranges(adj):
        img = img.point(_tone_lut(adj))
    if saturation != 1:
        img = img.convert("RGB", _saturation_matrix(saturation))
    if alpha is not None:
        img.putalpha(alpha)
    return img

def _auto_levels(img):
    """Sugiere ajustes a partir del histograma. El cliente los coloca en los
    sliders, asi la vista previa y el resultado guardado coinciden siempre."""
    small = _downscaled(img, (256, 256))
    if small.mode != "RGB":
        small = small.convert("RGB")

    def percentiles(hist, cut=0.005):
        """Extremos reales de un histograma, ignorando el 0,5% de cada punta
        (motas de polvo y brillos aislados no deben marcar el rango)."""
        total = sum(hist) or 1
        lo, hi, acc = 0, 255, 0
        for v, n in enumerate(hist):
            acc += n
            if acc >= total * cut:
                lo = v
                break
        acc = 0
        for v in range(255, -1, -1):
            acc += hist[v]
            if acc >= total * cut:
                hi = v
                break
        return lo, max(lo + 8, hi)

    # Equilibrio de color: cada canal se estira con SU propio rango. Esto
    # devuelve el contraste y quita la dominante de color a la vez.
    channels = small.split()[:3]
    wb = {}
    for key, ch in zip(("r", "g", "b"), channels):
        lo, hi = percentiles(ch.histogram())
        wb[key] = [lo, hi]

    # Con el estirado ya aplicado, ¿sigue oscura? Entonces levantar sombras
    gray_hist = small.convert("L").histogram()
    total = sum(gray_hist) or 1
    mean = sum(v * n for v, n in enumerate(gray_hist)) / total
    lo_l, hi_l = percentiles(gray_hist)
    mean_norm = (mean - lo_l) / max(1.0, hi_l - lo_l)
    shadows = 0.0
    if mean_norm < 0.45:
        shadows = min(65.0, (0.45 - mean_norm) * 220.0)

    return {
        "wb": wb,
        "black": 0.0,
        "white": 255.0,
        "shadows": round(shadows, 1),
        "highlights": 0.0,
        "temp": 0.0,
        "tint": 0.0,
        "brightness": 1.0,
        "contrast": 1.0,
        "saturation": 1.0,
    }

# ── Enderezado ───────────────────────────────────────────────────────────────
# Al enderezar quedarian esquinas vacias, asi que se recorta al mayor
# rectangulo centrado con la MISMA proporcion que quepa dentro de la imagen
# girada. El factor solo depende de la proporcion y del angulo, asi que el
# editor del navegador calcula exactamente el mismo encuadre.
def _straighten_scale(w, h, angle):
    rad = math.radians(abs(float(angle)))
    c, sn = math.cos(rad), math.sin(rad)
    return min(w / (w * c + h * sn), h / (w * sn + h * c))

def _straighten(img, angle):
    angle = float(angle)
    if not angle:
        return img
    w, h = img.size
    k = _straighten_scale(w, h, angle)
    # Angulo positivo = sentido horario (Pillow gira antihorario)
    rot = img.rotate(-angle, resample=Image.BICUBIC, expand=False)
    nw = max(1, int(round(w * k)))
    nh = max(1, int(round(h * k)))
    x0 = (w - nw) // 2
    y0 = (h - nh) // 2
    return rot.crop((x0, y0, x0 + nw, y0 + nh))

# ── Nitidez y suavizado ──────────────────────────────────────────────────────
# Una sola mascara de enfoque sirve para las dos direcciones:
#   amount > 0  ->  enfocar    (realza la diferencia con el desenfoque)
#   amount < 0  ->  suavizar   (acerca la imagen al desenfoque: quita grano)
# El radio va con la resolucion, de modo que la vista previa (mas pequena)
# muestra el mismo efecto relativo que el archivo final.
SHARPEN_RADIUS_DIV = 700
# Una sola pasada de caja: dos pasadas quedaban algo mas suaves pero doblaban
# el coste en el navegador, y con estos radios la diferencia no se aprecia
SHARPEN_PASSES = 1

def _blur_radius(w, h):
    return max(1, int(round(max(w, h) / SHARPEN_RADIUS_DIV)))

def _apply_sharpen(img, amount):
    a = max(-100.0, min(150.0, float(amount or 0))) / 100.0
    if a == 0:
        return img
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    r = _blur_radius(*img.size)
    blurred = img
    for _ in range(SHARPEN_PASSES):
        blurred = blurred.filter(ImageFilter.BoxBlur(r))
    mag = abs(a)
    scale = lambda v: min(255, int(mag * v + 0.5))
    hi = ImageChops.subtract(img, blurred).point(scale)   # donde la imagen supera al desenfoque
    lo = ImageChops.subtract(blurred, img).point(scale)   # donde queda por debajo
    # Cada una de estas imagenes es una COPIA ENTERA de la foto: 144 MB en una
    # de 48 Mpx. Encadenandolas todas en una sola linea seguian vivas a la vez
    # y enfocar se iba por encima del giga; soltandolas en cuanto sobran, el
    # resultado es identico al pixel y caben cuatro donde antes seis.
    del blurred
    if a > 0:
        salida = ImageChops.add(img, hi)
        del hi
        return ImageChops.subtract(salida, lo)
    salida = ImageChops.subtract(img, hi)
    del hi
    return ImageChops.add(salida, lo)

def _open_for_edit(abs_path, box=None):
    """Abre la imagen ya orientada (como se ve en el visor). Para un RAW usa la
    vista previa incrustada, que es lo unico editable de ese formato.

    Con `box` se abre solo a la resolucion que hace falta. Guardar una edicion
    necesita la foto entera, pero MIRARLA no: descomprimir 48 Mpx (144 MB) para
    sacar un histograma de 512 px es tirar la memoria del NAS a la basura."""
    ext = abs_path.suffix.lower()
    if ext in RAW_EXTS:
        data = _extract_raw_preview(abs_path)
        if not data:
            raise Exception("no se pudo leer la vista previa del RAW")
        img = Image.open(io.BytesIO(data))
        if box:
            _draft(img, box)
            img.thumbnail(box)
        op = ORIENTATION_OPS.get(_read_orientation(abs_path))
        if op is not None:
            img = img.transpose(op)
        return img
    if box:
        return _fit(_open_scaled(abs_path, box), box)
    img = Image.open(str(abs_path))
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img

def _save_pixels(img, dest, ext, exif=None):
    """
    Guarda la imagen en el formato que corresponde a la extension.
    Si se pasa el bloque EXIF del original, va incrustado DE UNA VEZ: escribir
    los metadatos despues, con ExifTool, obliga a reescribir el archivo entero
    (casi 3 s en un PNG de 23 MB).
    En PNG no se usa optimize: prueba varias estrategias de compresion para
    ahorrar un 2% de tamano, y con archivos grandes eso se nota y no compensa.
    """
    extra = {"exif": exif} if exif else {}
    if ext in (".jpg", ".jpeg"):
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(str(dest), format="JPEG", quality=95, subsampling=0, optimize=True, **extra)
    elif ext == ".png":
        img.save(str(dest), format="PNG", compress_level=6, **extra)
    elif ext == ".webp":
        img.save(str(dest), format="WEBP", quality=95, method=4, **extra)
    elif ext in (".tif", ".tiff"):
        # LZW es sin perdida y evita TIFF enormes sin comprimir
        img.save(str(dest), format="TIFF", compression="tiff_lzw")
    elif ext in HEIC_EXTS:
        img.save(str(dest), format="HEIF", quality=95)
    else:
        raise Exception("formato no editable")

def _source_exif(abs_path):
    """Bloque EXIF del original listo para incrustar en el archivo editado.
    Se conserva tal cual (GPS y fechas incluidos) y solo se fuerza
    Orientation=1, porque el giro ya va aplicado a los pixeles."""
    try:
        with Image.open(str(abs_path)) as im:      # no decodifica los pixeles
            exif = im.getexif()
            if not exif:
                return None
            exif[0x0112] = 1
            data = exif.tobytes()
        return data or None
    except Exception:
        return None

def _needs_metadata_copy(abs_path, exif_embedded):
    """
    Decide si hace falta pasar por ExifTool, que es lo caro (reescribe el
    archivo entero: casi 3 s en un PNG de 23 MB). Leer es barato.
    - Si ya se incrusto el EXIF al guardar, solo importan XMP e IPTC.
    - Si no se pudo leer el EXIF (un RAW, por ejemplo), tambien hace falta el.
    Un PNG sin metadatos -un recorte cualquiera- no necesita nada.
    """
    groups = ["-XMP:all", "-IPTC:all"]
    if not exif_embedded:
        groups.append("-EXIF:all")
    try:
        result = subprocess.run(
            ["exiftool", "-fast2", "-s3"] + groups + [str(abs_path)],
            capture_output=True, text=True, timeout=30)
        return bool(result.stdout.strip())
    except Exception:
        return True     # ante la duda, la via segura

def _copy_metadata(src, dest):
    """Copia los metadatos del original al archivo editado (GPS, fechas, camara)
    dejando fuera lo que ya no cuadra: orientacion, tamano y miniaturas viejas."""
    subprocess.run(
        ["exiftool", "-tagsFromFile", str(src), "-all:all",
         "--Orientation", "--ThumbnailImage", "--PreviewImage",
         "--ExifImageWidth", "--ExifImageHeight",
         "-overwrite_original", str(dest)],
        capture_output=True, text=True, timeout=60
    )
    # La orientacion ya esta aplicada a los pixeles
    subprocess.run(["exiftool", "-Orientation=1", "-n", "-overwrite_original", str(dest)],
                   capture_output=True, text=True, timeout=30)

def _edit_destination(path):
    """Donde se guarda la edicion: sobre el original salvo que el formato no
    admita reescritura (RAW), en cuyo caso se crea un JPEG derivado."""
    ext = path.suffix.lower()
    if ext in EDITABLE_EXTS or ext in (".tif", ".tiff"):
        return path, False
    candidate = path.with_name(path.stem + "_edit.jpg")
    n = 2
    while candidate.exists():
        candidate = path.with_name(path.stem + "_edit_" + str(n) + ".jpg")
        n += 1
    return candidate, True

def _prewarm_cache(img, dest):
    """Deja hechas la miniatura y la vista previa del archivo recien guardado.
    El navegador las pide justo despues de editar y, si no estan, hay que
    decodificar otra vez la foto entera."""
    try:
        preview = _downscaled(img, (PREVIEW_MAX, PREVIEW_MAX))
        if preview.mode not in ("RGB", "RGBA", "L"):
            preview = preview.convert("RGB")
        buf = io.BytesIO()
        preview.save(buf, format="WEBP", quality=88, method=PREVIEW_WEBP_METHOD)
        _cache_store(_thumb_cache_path(dest, "PREVIEW-WEBP"), buf.getvalue())
        thumb = _downscaled(preview, (300, 300))
        buf = io.BytesIO()
        thumb.save(buf, format="WEBP", quality=82, method=4)
        _cache_store(_thumb_cache_path(dest, "WEBP"), buf.getvalue())
    except Exception:
        pass

def _save_edit(img, abs_path):
    """Guarda una imagen editada: escribe primero un temporal, le pone los
    metadatos del original y solo entonces reemplaza el archivo. Asi un fallo a
    medias nunca deja el original corrupto. Devuelve (destino, es_derivado).

    Los metadatos van incrustados al guardar (rapido). Solo se pasa por ExifTool
    cuando hace falta de verdad: si el original lleva XMP o IPTC, o si no se
    pudo leer su EXIF (por ejemplo en un RAW, que se guarda como JPEG derivado).
    """
    dest, derived = _edit_destination(abs_path)
    tmp = dest.with_name("." + dest.name + ".edit_tmp" + dest.suffix)
    exif = _source_exif(abs_path)
    try:
        _save_pixels(img, tmp, dest.suffix.lower(), exif=exif)
        if _needs_metadata_copy(abs_path, exif is not None):
            _copy_metadata(abs_path, tmp)
        os.replace(str(tmp), str(dest))
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise
    _trigger_reindex(dest)
    _evict_thumb_cache(dest)
    _evict_thumb_cache(abs_path)
    _prewarm_cache(img, dest)
    _index_stale(dest)
    return dest, derived

def _unique_target(dest_dir, name):
    """Nombre libre dentro de dest_dir, anadiendo un sufijo si hace falta."""
    candidate = dest_dir / name
    stem, ext = os.path.splitext(name)
    n = 2
    while candidate.exists():
        candidate = dest_dir / (stem + "_" + str(n) + ext)
        n += 1
    return candidate

# =============================================================================
# INDICE DE METADATOS
# Leer el GPS de cada foto con ExifTool es proporcional al numero de archivos
# (y un RAW tarda bastante en abrirse), asi que el mapa y el filtro "sin GPS"
# se sirven de un indice en SQLite que se mantiene en segundo plano: cada foto
# se lee UNA vez, y solo se vuelve a leer si cambia su fecha o su tamano.
# El indice es una cache: si se borra, se reconstruye solo.
# =============================================================================

INDEX_FILE = SETTINGS_DIR + "/index.db"
INDEX_BATCH = 150          # fotos por llamada a ExifTool
INDEX_INTERVAL = 900       # repaso periodico del arbol (segundos)

_index_conn = None
_index_lock = threading.Lock()
# SQLite admite muchos lectores pero un solo escritor: las escrituras se
# serializan aqui para no chocar con el repaso de fondo ("database is locked")
_index_write = threading.Lock()
_index_state = {"scanning": False, "done": 0, "total": 0, "root": "", "at": 0}

def _index_db():
    """Una sola conexion compartida. Flask crea un HILO POR PETICION, asi que
    una conexion por hilo significaba abrir la base en cada peticion."""
    global _index_conn
    conn = _index_conn
    if conn is None:
        conn = sqlite3.connect(INDEX_FILE, timeout=60, check_same_thread=False)
        try:
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("PRAGMA journal_mode=WAL")   # lecturas mientras se escribe
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass                                     # WAL es una mejora, no un requisito
        with _index_write:
            conn.execute("""CREATE TABLE IF NOT EXISTS photos (
                path TEXT PRIMARY KEY,
                mtime INTEGER, size INTEGER,
                lat REAL, lon REAL, date TEXT,
                blur TEXT)""")
            try:
                # Bases de datos de versiones anteriores: se les anade la
                # columna y el proximo repaso la va rellenando.
                conn.execute("ALTER TABLE photos ADD COLUMN blur TEXT")
            except sqlite3.OperationalError:
                pass                                 # ya la tiene
            # Nombres de lugar ya consultados: Nominatim obliga a esperar un
            # segundo entre peticiones, asi que conviene no repetirlas nunca
            conn.execute("""CREATE TABLE IF NOT EXISTS places (
                key TEXT PRIMARY KEY, name TEXT, at INTEGER)""")
            conn.commit()
        _index_conn = conn
    return conn

def _index_range(base):
    """Limites para consultar por prefijo de ruta sin usar LIKE (una ruta puede
    llevar % o _, que en LIKE son comodines)."""
    prefix = str(base).rstrip("/") + "/"
    return prefix, prefix + "\uffff"

def _index_walk(base):
    """Fotos del arbol con su fecha y tamano, saltando ocultas y del sistema."""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and not d.startswith("@")]
        for fn in filenames:
            if fn.startswith(".") or fn.startswith("@"):
                continue
            if os.path.splitext(fn)[1].lower() not in SUPPORTED_EXTS:
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            yield full, int(st.st_mtime), st.st_size

# =============================================================================
# LA MINIATURA DIMINUTA (el truco de Google Photos)
# Una rejilla llena de huecos grises PARECE lenta aunque no lo sea. Lo que hace
# que Google Photos parezca instantaneo no es que sus miniaturas vuelen: es que
# nunca ensena un hueco. Manda, dentro del propio listado, una version de 16 px
# de cada foto -142 bytes- que el navegador pinta borrosa al instante, y encima
# de ella va apareciendo la miniatura de verdad.
#
# Aqui sale practicamente gratis porque casi todas las fotos de camara o movil
# llevan YA una miniatura de 160x120 incrustada en su EXIF, y el repaso del
# indice recorre todos los archivos con ExifTool de todas formas: pedirsela en
# la misma llamada cuesta un 5% mas (0,208 s -> 0,218 s por cada 80 fotos) y
# encogerla a 16 px, 0,3 ms. Las fotos que no la traen (escaneos, PNG, algunas
# ya editadas) la consiguen igual la primera vez que se genera su miniatura.
# =============================================================================

LQIP_PX = 16                # lado mayor de la version diminuta
LQIP_QUALITY = 55
LQIP_LISTADO_MAX = 1500     # cuantas viajan en un listado

def _lqip(img):
    """Version de 16 px en WebP, lista para incrustar en el listado."""
    try:
        tiny = _downscaled(img, (LQIP_PX, LQIP_PX))
        if tiny.mode not in ("RGB", "L"):
            tiny = tiny.convert("RGB")
        buf = io.BytesIO()
        tiny.save(buf, format="WEBP", quality=LQIP_QUALITY, method=6)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None

def _lqip_from_bytes(data):
    """Lo mismo, a partir de la miniatura incrustada que devuelve ExifTool."""
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            return _lqip(img)
    except Exception:
        return None

def _index_read_batch(paths):
    """Lee un lote con UNA sola llamada a ExifTool."""
    # -fast2 corta la lectura en cuanto tiene los metadatos: en un RAW o un PNG
    # grande evita leerse el archivo entero. -b devuelve la miniatura
    # incrustada en base64 dentro del mismo JSON.
    cmd = ["exiftool", "-fast2", "-json", "-n", "-q", "-q", "-b",
           "-ThumbnailImage",
           "-GPSLatitude", "-GPSLongitude", "-DateTimeOriginal", "-SourceFile"] + paths
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        items = json.loads(result.stdout) if result.stdout.strip() else []
    except Exception:
        items = []
    read = {}
    for item in items:
        src = item.get("SourceFile")
        if not src:
            continue
        lat, lon = item.get("GPSLatitude"), item.get("GPSLongitude")
        try:
            lat = float(lat) if lat is not None else None
            lon = float(lon) if lon is not None else None
        except (TypeError, ValueError):
            lat = lon = None
        crudo = item.get("ThumbnailImage") or ""
        if crudo.startswith("base64:"):
            try:
                crudo = base64.b64decode(crudo[7:])
            except Exception:
                crudo = b""
        else:
            crudo = b""
        read[os.path.abspath(src)] = (lat, lon, _clean_date(item.get("DateTimeOriginal")),
                                      _lqip_from_bytes(crudo))
    return read

def _index_scan(base):
    """Pone al dia el indice de un arbol: solo lee lo nuevo o lo que cambio."""
    conn = _index_db()
    lo, hi = _index_range(base)

    current = {}
    for full, mtime, size in _index_walk(base):
        current[full] = (mtime, size)

    known = {}
    for path, mtime, size in conn.execute(
            "SELECT path, mtime, size FROM photos WHERE path >= ? AND path < ?", (lo, hi)):
        known[path] = (mtime, size)

    # Ojo: no basta con "no estaba en el recorrido". Una foto copiada mientras
    # se recorria el arbol no aparece en esa foto fija, y borrarla la sacaria
    # del indice hasta el siguiente repaso. Se comprueba en disco.
    gone = [p for p in known if p not in current and not os.path.exists(p)]
    if gone:
        with _index_write:
            conn.executemany("DELETE FROM photos WHERE path = ?", [(p,) for p in gone])
            conn.commit()

    todo = [p for p, v in sorted(current.items()) if known.get(p) != v]
    _index_state.update(total=len(todo), done=0, root=str(base))
    for i in range(0, len(todo), INDEX_BATCH):
        batch = todo[i:i + INDEX_BATCH]
        read = _index_read_batch(batch)
        rows = []
        for path in batch:
            lat, lon, date, blur = read.get(path, (None, None, "", None))
            mtime, size = current[path]
            rows.append((path, mtime, size, lat, lon, date, blur))
        with _index_write:
            conn.executemany(
                "INSERT OR REPLACE INTO photos (path, mtime, size, lat, lon, date, blur) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            conn.commit()
        _index_state["done"] += len(batch)
        # Un respiro entre lotes: el repaso es de fondo y no debe comerse el
        # disco mientras se esta trabajando con las fotos
        time.sleep(0.15)
    _index_state["at"] = int(time.time())

def _index_worker(base):
    try:
        _index_scan(Path(base))
        _blur_flush()
        _cache_sweep()
    except Exception as e:
        print("Index error:", str(e))
    finally:
        _index_state["scanning"] = False

INDEX_MIN_GAP = 60         # segundos entre repasos del mismo arbol

def _index_request(base, force=False):
    """Lanza un repaso en segundo plano si no hay otro en marcha. Recorrer el
    arbol cuesta E/S en el NAS, asi que no se repite si acaba de hacerse."""
    with _index_lock:
        if _index_state["scanning"]:
            return False
        if (not force and _index_state["at"]
                and time.time() - _index_state["at"] < INDEX_MIN_GAP
                and _index_state["root"] == str(base)):
            return False
        _index_state["scanning"] = True
    threading.Thread(target=_index_worker, args=(str(base),), daemon=True).start()
    return True

def _index_photos(base):
    """Fotos CON coordenadas bajo base, tal como las conoce el indice."""
    lo, hi = _index_range(base)
    return _index_db().execute(
        "SELECT path, lat, lon, date, mtime FROM photos "
        "WHERE path >= ? AND path < ? AND lat IS NOT NULL AND lon IS NOT NULL",
        (lo, hi)).fetchall()

def _index_dates(folder):
    """Lo que el indice sabe de las fotos de una carpeta: la fecha de los
    metadatos y la miniatura diminuta, con la fecha y el tamano del archivo
    para poder comprobar que sigue al dia. Si una fila guarda algo que no es
    una fecha se descarta y se marca para releer: asi una fila envenenada no se
    queda para siempre."""
    out, sucias = {}, []
    try:
        lo, hi = _index_range(folder)
        for path, mtime, size, date, blur in _index_db().execute(
                "SELECT path, mtime, size, date, blur FROM photos "
                "WHERE path >= ? AND path < ?", (lo, hi)).fetchall():
            if date and not _clean_date(date):
                sucias.append(path)
                continue
            out[path] = (mtime, size, date or "", blur)
    except Exception:
        pass
    for path in sucias:
        _index_stale(path)
    return out

# Un commit por foto sale mas caro que generar la propia miniatura diminuta,
# sobre todo con el pre-generado y el repaso del indice escribiendo a la vez
# (comparten el mismo cerrojo de escritura). Se acumulan y se guardan en lotes;
# si se pierde alguno al reiniciar no pasa nada, se vuelve a calcular.
BLUR_BATCH = 32
_blur_pend = []
_blur_pend_lock = threading.Lock()

def _index_set_blur(path, blur):
    """Apunta la miniatura diminuta de una foto que no la traia incrustada."""
    if not blur:
        return
    with _blur_pend_lock:
        _blur_pend.append((blur, str(path)))
        lleno = len(_blur_pend) >= BLUR_BATCH
    if lleno:
        _blur_flush()

def _blur_flush():
    with _blur_pend_lock:
        if not _blur_pend:
            return
        lote = list(_blur_pend)
        del _blur_pend[:]
    try:
        conn = _index_db()
        with _index_write:
            conn.executemany("UPDATE photos SET blur = ? WHERE path = ?", lote)
            conn.commit()
    except Exception:
        pass

def _index_without_gps(base):
    """Fotos que el indice sabe que NO tienen coordenadas."""
    lo, hi = _index_range(base)
    return _index_db().execute(
        "SELECT path, mtime, date FROM photos WHERE path >= ? AND path < ? AND lat IS NULL",
        (lo, hi)).fetchall()

# ~1,1 km: el nombre que devuelve Nominatim a este nivel de zoom es el del
# pueblo o barrio, asi que afinar mas solo multiplica las consultas
PLACE_PRECISION = 2

def _place_key(lat, lon):
    return "%.*f,%.*f" % (PLACE_PRECISION, lat, PLACE_PRECISION, lon)

def _place_cached(key):
    try:
        row = _index_db().execute("SELECT name FROM places WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None

def _place_store(key, name):
    try:
        conn = _index_db()
        with _index_write:
            conn.execute("INSERT OR REPLACE INTO places (key, name, at) VALUES (?,?,?)",
                         (key, name, int(time.time())))
            conn.commit()
    except Exception:
        pass

def _index_get(path):
    """Lo que el indice sabe de una foto, SOLO si sigue al dia (misma fecha y
    tamano). Evita lanzar ExifTool para algo que ya se leyo una vez."""
    try:
        st = os.stat(str(path))
        row = _index_db().execute(
            "SELECT mtime, size, lat, lon, date FROM photos WHERE path = ?",
            (str(path),)).fetchone()
        if not row or row[0] != int(st.st_mtime) or row[1] != st.st_size:
            return None
        return {"lat": row[2], "lon": row[3], "date": row[4] or "",
                "file_mtime": int(st.st_mtime)}
    except Exception:
        return None

def _index_count(base):
    lo, hi = _index_range(base)
    return _index_db().execute(
        "SELECT COUNT(*) FROM photos WHERE path >= ? AND path < ?", (lo, hi)).fetchone()[0]

# ── Mantener el indice al dia cuando la app toca los archivos ───────────────
def _index_set_gps(path, lat, lon):
    """Tras escribir GPS ya sabemos las coordenadas: no hace falta releerlas."""
    try:
        st = os.stat(str(path))
        conn = _index_db()
        with _index_write:
            conn.execute(
                "INSERT INTO photos (path, mtime, size, lat, lon, date) VALUES (?,?,?,?,?,"
                "COALESCE((SELECT date FROM photos WHERE path = ?), '')) "
                "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size, "
                "lat=excluded.lat, lon=excluded.lon",
                (str(path), int(st.st_mtime), st.st_size, lat, lon, str(path)))
            conn.commit()
    except Exception:
        pass

def _index_set_date(path, exif_date):
    """Tras fijar una fecha concreta ya la conocemos: se anota en el indice en
    vez de marcar la foto para releerla."""
    try:
        st = os.stat(str(path))
        conn = _index_db()
        with _index_write:
            conn.execute(
                "UPDATE photos SET mtime = ?, size = ?, date = ? WHERE path = ?",
                (int(st.st_mtime), st.st_size, exif_date, str(path)))
            conn.commit()
    except Exception:
        pass

def _index_stale(path):
    """Marca una foto para que el proximo repaso la relea (sin perder lo que ya
    sabemos de ella, que sigue siendo valido para el mapa)."""
    try:
        conn = _index_db()
        with _index_write:
            conn.execute("UPDATE photos SET mtime = -1 WHERE path = ?", (str(path),))
            conn.commit()
    except Exception:
        pass

def _index_forget(path):
    try:
        conn = _index_db()
        with _index_write:
            conn.execute("DELETE FROM photos WHERE path = ?", (str(path),))
            conn.commit()
    except Exception:
        pass

def _index_move(old, new):
    """Mover o renombrar no cambia los metadatos: basta con mover la fila."""
    try:
        st = os.stat(str(new))
        conn = _index_db()
        with _index_write:
            conn.execute("DELETE FROM photos WHERE path = ?", (str(new),))
            conn.execute("UPDATE photos SET path = ?, mtime = ? WHERE path = ?",
                         (str(new), int(st.st_mtime), str(old)))
            conn.commit()
    except Exception:
        pass

def _index_copy(old, new):
    try:
        st = os.stat(str(new))
        conn = _index_db()
        with _index_write:
            conn.execute(
                "INSERT OR REPLACE INTO photos (path, mtime, size, lat, lon, date, blur) "
                "SELECT ?, ?, ?, lat, lon, date, blur FROM photos WHERE path = ?",
                (str(new), int(st.st_mtime), st.st_size, str(old)))
            conn.commit()
    except Exception:
        pass

def _index_background():
    """Repaso periodico de la carpeta de trabajo, mas uno al arrancar."""
    time.sleep(3)
    while True:
        try:
            root = _work_root()
            if root.is_dir():
                _index_request(root)
        except Exception:
            pass
        time.sleep(INDEX_INTERVAL)

threading.Thread(target=_index_background, daemon=True).start()

def _reverse_geocode(lat, lon):
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 14, "addressdetails": 1},
            headers={
                "User-Agent": "GeoTagger-QNAP/1.0 (personal photo tagger)",
                "Accept": "application/json",
                "Accept-Language": "es,en"
            },
            timeout=10
        )
        if r.status_code != 200 or not r.text.strip():
            return None
        data = r.json()
        addr = data.get("address", {})
        for key in ["village", "town", "city", "hamlet", "suburb", "municipality", "county", "state"]:
            if addr.get(key):
                return addr[key]
        dn = data.get("display_name", "")
        if dn:
            return dn.split(",")[0]
    except Exception as e:
        print("Reverse geocode error:", str(e))
    return None

def _thumb_cache_path(abs_path, fmt="JPEG"):
    """Nombre estable a partir de ruta + fecha + formato. El prefijo depende
    SOLO de la ruta, para poder borrar todas las versiones de una foto."""
    try:
        mtime = str(int(os.path.getmtime(abs_path) * 1000))
    except Exception:
        mtime = "0"
    key = _cache_key(abs_path)
    stamp = hashlib.md5((mtime + fmt).encode()).hexdigest()[:12]
    ext = ".webp" if "WEBP" in fmt else ".jpg"
    folder = _cache_dir_for(key)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return folder / (key[:16] + "_" + stamp + ext)

def _clean_flat_cache():
    """Limpieza unica de la cache antigua (todo en una carpeta). Son ficheros
    regenerables y ocupaban espacio sin poder borrarse nunca."""
    try:
        root = Path(THUMB_CACHE_DIR)
        for f in root.iterdir():
            if f.is_file() and f.suffix in (".jpg", ".webp"):
                f.unlink(missing_ok=True)
    except Exception:
        pass

# Limpieza unica de la cache antigua, ya con la funcion definida
_clean_flat_cache()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    if request.method == "GET":
        return jsonify(_load_settings())
    data = request.json
    _save_settings(data)
    return jsonify({"ok": True})

# Se mira solo una parte de la carpeta: en un NAS con miles de fotos no hace
# falta contarlas todas para decidir si esa carpeta interesa.
PHOTO_COUNT_CAP = 99
PHOTO_SCAN_CAP = 2000

def _count_photos(directory):
    """Cuenta (a ojo) cuantas fotos hay sueltas en una carpeta."""
    n = 0
    try:
        with os.scandir(directory) as it:
            for i, entry in enumerate(it):
                if i >= PHOTO_SCAN_CAP or n >= PHOTO_COUNT_CAP:
                    break
                if entry.name.startswith(".") or entry.name.startswith("@"):
                    continue
                if entry.is_file() and os.path.splitext(entry.name)[1].lower() in SUPPORTED_EXTS:
                    n += 1
    except Exception:
        return 0
    return n

@app.route("/api/nas_browse")
def nas_browse():
    """Lista SOLO carpetas colgando de la raiz del NAS. Sirve para elegir la
    carpeta de trabajo, asi que no depende de la que este activa."""
    rel = (request.args.get("path", "") or "").strip().strip("/")
    abs_path = _safe_join(rel, NAS_ROOT)
    if abs_path is None or not abs_path.is_dir():
        return jsonify({"error": "la carpeta no existe"}), 404
    try:
        items = sorted(abs_path.iterdir())
    except PermissionError:
        return jsonify({"error": "sin permisos para leer esta carpeta"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    dirs = []
    for item in items:
        if item.name.startswith("@") or item.name.startswith("."):
            continue
        try:
            if not item.is_dir():
                continue
        except OSError:
            continue
        dirs.append({
            "name": item.name,
            "path": (rel + "/" + item.name) if rel else item.name,
            "photos": _count_photos(item),
        })

    parent = "/".join(rel.split("/")[:-1]) if rel else None
    info = _work_root_info()
    return jsonify({
        "current": rel,
        "parent": parent,
        "dirs": dirs,
        "photos": _count_photos(abs_path),
        "nas_root": NAS_ROOT,
        "nas_name": info["nas_name"],
        "work_root": info["path"],
    })

@app.route("/api/work_root", methods=["GET", "POST"])
def work_root():
    """Consulta o cambia la carpeta de trabajo (la raiz de la galeria)."""
    if request.method == "GET":
        return jsonify(_work_root_info())
    data = request.json or {}
    rel = (data.get("path", "") or "").strip().strip("/")
    target = _safe_join(rel, NAS_ROOT)
    if target is None or not target.is_dir():
        return jsonify({"error": "la carpeta no existe"}), 400
    _save_settings({"work_root": rel})
    _index_request(target, force=True)
    return jsonify(_work_root_info())

@app.route("/api/favorites", methods=["GET", "POST"])
def favorites():
    """Lista, anade o quita accesos directos a carpetas."""
    if request.method == "GET":
        return jsonify({"favorites": _favorites()})

    data = request.json or {}
    action = data.get("action", "add")
    favs = _favorites()

    if action == "remove":
        path = str(data.get("path", "")).strip().strip("/")
        favs = [f for f in favs if f["path"] != path]
        _save_settings({"favorites": favs})
        return jsonify({"favorites": favs})

    # Anadir: llega la ruta relativa a la carpeta de trabajo y se guarda
    # completa (relativa al NAS), que es lo que hace falta para volver a ella
    rel = str(data.get("path", "")).strip().strip("/")
    target = _resolve_path(rel)
    if target is None or not target.is_dir():
        return jsonify({"error": "la carpeta no existe"}), 404
    base = Path(NAS_ROOT).resolve()
    try:
        full = "" if target == base else str(target.relative_to(base))
    except ValueError:
        return jsonify({"error": "fuera del NAS"}), 400
    if any(f["path"] == full for f in favs):
        return jsonify({"favorites": favs, "already": True})
    name = str(data.get("name", "")).strip() or (full.split("/")[-1] if full else (base.name or "NAS"))
    if len(favs) >= 40:
        return jsonify({"error": "demasiados favoritos"}), 400
    favs.append({"path": full, "name": name})
    _save_settings({"favorites": favs})
    return jsonify({"favorites": favs})

@app.route("/api/browse")
def browse():
    rel = request.args.get("path", "")
    abs_path = _resolve_path(rel)
    if abs_path is None or not abs_path.exists():
        return jsonify({"error": "Ruta no existe"}), 404
    dirs, files = [], []
    # La fecha que importa al filtrar y ordenar es la de la FOTO, no la del
    # archivo: una foto de 2005 copiada al NAS tiene fecha de archivo de hoy.
    # El indice ya la sabe, asi que no cuesta nada acompanarla.
    _blur_flush()          # que lo recien generado viaje ya en este listado
    fechas = _index_dates(abs_path)
    for item in sorted(abs_path.iterdir()):
        if item.name.startswith("@") or item.name.startswith("."):
            continue
        if item.is_dir():
            dirs.append({"name": item.name, "path": str(Path(rel) / item.name)})
        elif item.suffix.lower() in SUPPORTED_EXTS:
            try:
                st = item.stat()
                mtime, size = int(st.st_mtime), st.st_size
            except Exception:
                mtime, size = 0, -1
            fila = fechas.get(str(item))
            # Solo vale si la foto no ha cambiado desde que se indexo
            al_dia = fila and fila[0] == mtime and fila[1] == size
            files.append({
                "name": item.name,
                "path": str(Path(rel) / item.name),
                "ext": item.suffix.lower(),
                "mtime": mtime,
                "date": _ui_date(fila[2]) if al_dia else "",
                # 142 bytes que evitan un hueco gris (ver LA MINIATURA DIMINUTA)
                "blur": (fila[3] or "") if al_dia else "",
            })
    # Las fotos mas recientes primero (por fecha de modificacion del archivo).
    files.sort(key=lambda f: f["mtime"], reverse=True)
    # La galeria avisa de que va a ENSENAR estas fotos, asi que sus miniaturas
    # se van haciendo de fondo en este mismo orden. El selector de carpeta de
    # destino usa la misma ruta y no lo pide: alli solo se ven carpetas.
    if request.args.get("prefetch") == "1":
        _prefetch_request(abs_path, [f["name"] for f in files])
    # Las diminutas solo de lo que se va a llegar a ver de una sentada: en una
    # carpeta de 3.000 fotos serian 0,6 MB de listado, y para cuando se baja
    # tanto las miniaturas de verdad ya estan hechas.
    for f in files[LQIP_LISTADO_MAX:]:
        f["blur"] = ""
    return jsonify({"dirs": dirs, "files": files, "current": rel})

@app.route("/api/thumb")
def thumb():
    """
    Genera una miniatura EN MEMORIA para mostrar en la interfaz.
    Usa caché en disco para evitar regenerar en cada petición.
    Sirve WebP cuando el navegador lo soporta (25-35% más pequeño).
    NUNCA escribe sobre el archivo original.
    """
    rel = request.args.get("path", "")
    abs_path = _resolve_path(rel)
    if abs_path is None or not abs_path.exists():
        return "", 404

    accept = request.headers.get("Accept", "")
    fmt = "WEBP" if "image/webp" in accept else "JPEG"
    mime = "image/webp" if fmt == "WEBP" else "image/jpeg"

    cache_file = _thumb_cache_path(abs_path, fmt)

    if cache_file.exists():
        if request.headers.get("If-None-Match") == cache_file.stem:
            return "", 304
        return _cached_image_response(cache_file, mime)

    try:
        # Una sola generacion por miniatura aunque la pidan varias peticiones
        with _render_once(str(cache_file)):
            if cache_file.exists():     # la genero otro hilo mientras esperabamos
                return _cached_image_response(cache_file, mime)
            with _user_decode():
                _make_thumb(abs_path, cache_file, fmt)
        return _cached_image_response(cache_file, mime)
    except Exception as e:
        return str(e), 404

def _make_thumb(abs_path, cache_file, fmt):
    """Genera la miniatura y la deja en la cache. NUNCA toca el original."""
    ext = abs_path.suffix.lower()
    box = (300, 300)
    if ext in RAW_EXTS:
        result = subprocess.run(
            ["exiftool", "-b", "-ThumbnailImage", str(abs_path)],
            capture_output=True, timeout=15
        )
        if result.returncode == 0 and result.stdout:
            if fmt != "WEBP":
                _cache_store(cache_file, result.stdout)
                return
            # La miniatura incrustada viene en JPEG: pasarla a WebP
            img = Image.open(io.BytesIO(result.stdout))
            buf = io.BytesIO()
            img.save(buf, format="WEBP", quality=82, method=4)
            _cache_store(cache_file, buf.getvalue())
            return

    img = _fit(_open_scaled(abs_path, box), box)
    # Ya esta descomprimida y pequena: sacar aqui la version diminuta es casi
    # gratis, y asi tambien la tienen las fotos sin miniatura EXIF incrustada
    # (escaneos, PNG, las que salen de editar).
    _index_set_blur(abs_path, _lqip(img))
    buf = io.BytesIO()
    if fmt == "WEBP":
        img.save(buf, format="WEBP", quality=82, method=4)
    else:
        # JPEG no admite canal alfa (PNG/WebP RGBA) ni paleta
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=82, optimize=True)
    _cache_store(cache_file, buf.getvalue())

# Mapa de valores EXIF Orientation -> operacion de transposicion de Pillow.
# Se usa para orientar los previews extraidos de RAW, que no llevan EXIF propio.
ORIENTATION_OPS = {
    2: Image.FLIP_LEFT_RIGHT,
    3: Image.ROTATE_180,
    4: Image.FLIP_TOP_BOTTOM,
    5: Image.TRANSPOSE,
    6: Image.ROTATE_270,
    7: Image.TRANSVERSE,
    8: Image.ROTATE_90,
}

PREVIEW_MAX = 2048
PREVIEW_WEBP_METHOD = 2

def _extract_raw_preview(abs_path):
    """Extrae la vista previa JPEG embebida en un RAW (solo LECTURA).
    Prueba del preview mas grande al mas pequeno."""
    for tag in ("-JpgFromRaw", "-PreviewImage", "-ThumbnailImage"):
        try:
            result = subprocess.run(
                ["exiftool", "-b", tag, str(abs_path)],
                capture_output=True, timeout=20
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except Exception:
            pass
    return None

def _read_orientation(abs_path):
    """Lee el tag EXIF Orientation del archivo original (sin modificarlo)."""
    try:
        result = subprocess.run(
            ["exiftool", "-n", "-Orientation", "-s", "-s", "-s", str(abs_path)],
            capture_output=True, text=True, timeout=10
        )
        return int(result.stdout.strip())
    except Exception:
        return 1

def _cached_image_response(cache_file, mime):
    resp = send_file(str(cache_file), mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    resp.headers["ETag"] = cache_file.stem
    resp.headers["Vary"] = "Accept"
    return resp

@app.route("/api/preview")
def preview():
    """
    Genera EN MEMORIA una vista previa grande (max 2048px) para el visor de
    fotos. Convierte cualquier formato soportado (RAW, TIFF, HEIC, WebP...)
    a JPEG/WebP para que el navegador pueda mostrarlo.
    Usa la misma cache en disco que las miniaturas (clave por ruta + mtime).
    NUNCA escribe sobre el archivo original.
    """
    rel = request.args.get("path", "")
    abs_path = _resolve_path(rel)
    if abs_path is None or not abs_path.exists():
        return "", 404

    accept = request.headers.get("Accept", "")
    fmt = "WEBP" if "image/webp" in accept else "JPEG"
    mime = "image/webp" if fmt == "WEBP" else "image/jpeg"

    cache_file = _thumb_cache_path(abs_path, "PREVIEW-" + fmt)
    if cache_file.exists():
        if request.headers.get("If-None-Match") == cache_file.stem:
            return "", 304
        return _cached_image_response(cache_file, mime)

    try:
        with _render_once(str(cache_file)):
            if cache_file.exists():     # la genero otro hilo mientras esperabamos
                return _cached_image_response(cache_file, mime)
            with _user_decode():
                _make_preview(abs_path, cache_file, fmt)
        return _cached_image_response(cache_file, mime)
    except Exception as e:
        return str(e), 404

def _make_preview(abs_path, cache_file, fmt):
    """Genera la vista previa y la deja en la cache. NUNCA toca el original."""
    ext = abs_path.suffix.lower()
    box = (PREVIEW_MAX, PREVIEW_MAX)
    img = None
    if ext in RAW_EXTS:
        data = _extract_raw_preview(abs_path)
        if data:
            img = Image.open(io.BytesIO(data))
            _draft(img, box)
            img.thumbnail(box)
            # Los previews embebidos no llevan EXIF: aplicar la
            # orientacion declarada en el RAW original.
            op = ORIENTATION_OPS.get(_read_orientation(abs_path))
            if op is not None:
                img = img.transpose(op)
    if img is None:
        img = _fit(_open_scaled(abs_path, box), box)
    if fmt == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    if fmt == "WEBP":
        # method=4 tarda el doble que method=2 y aqui, a 2048 px, la diferencia
        # de tamano es de decimas de porcentaje: no compensa hacer esperar al
        # visor un cuarto de segundo mas por cada foto que se abre.
        img.save(buf, format="WEBP", quality=88, method=PREVIEW_WEBP_METHOD)
    else:
        img.save(buf, format="JPEG", quality=88, optimize=True)
    _cache_store(cache_file, buf.getvalue())

@app.route("/api/geocode")
def geocode():
    q = request.args.get("q", "").strip()
    if len(q) < 3:
        return jsonify([])
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "jsonv2", "limit": 5, "addressdetails": 0},
            headers={
                "User-Agent": "GeoTagger-QNAP/1.0 (personal photo tagger)",
                "Accept": "application/json",
                "Accept-Language": "es,en"
            },
            timeout=10
        )
        if r.status_code != 200 or not r.text.strip():
            return jsonify([])
        try:
            data = r.json()
        except Exception:
            return jsonify([])
        results = []
        for x in data:
            try:
                results.append({
                    "name": x.get("display_name", ""),
                    "lat": float(x["lat"]),
                    "lon": float(x["lon"])
                })
            except (KeyError, ValueError, TypeError):
                continue
        return jsonify(results)
    except Exception as e:
        print("Geocode error:", str(e))
        return jsonify([])

@app.route("/api/write", methods=["POST"])
def write_gps():
    """Escribe las mismas coordenadas en las fotos elegidas. Se hace por lotes:
    ExifTool acepta muchos archivos en una sola llamada, y arrancar un proceso
    por foto era lo que hacia lento aplicar GPS a una seleccion grande."""
    data = request.json
    lat   = float(data["lat"])
    lon   = float(data["lon"])
    alt   = float(data["alt"]) if data.get("alt") else None
    files = data["files"]

    targets, ok, errors = [], [], []
    for rel in files:
        path = _resolve_path(rel)
        if path is None or not path.exists():
            errors.append({"file": rel, "error": "no existe o ruta invalida"})
            continue
        if path.suffix.lower() not in SUPPORTED_EXTS:
            errors.append({"file": path.name, "error": "formato no soportado"})
            continue
        try:
            targets.append(sanitize_path(path))
        except Exception as e:
            errors.append({"file": path.name, "error": str(e)})

    args = ["-GPSLatitude=" + str(abs(lat)), "-GPSLatitudeRef=" + ("N" if lat >= 0 else "S"),
            "-GPSLongitude=" + str(abs(lon)), "-GPSLongitudeRef=" + ("E" if lon >= 0 else "W")]
    if alt is not None:
        args += ["-GPSAltitude=" + str(abs(alt)), "-GPSAltitudeRef=" + ("0" if alt >= 0 else "1")]

    for chunk in _chunks(targets, EXIFTOOL_CHUNK):
        try:
            result = _run_exiftool_batch(args, chunk)
        except Exception as e:
            for path in chunk:
                errors.append({"file": path.name, "error": str(e)})
            continue
        escritas = result.updated if result.returncode == 0 else 0
        if escritas != len(chunk):
            # Alguna se quedo fuera: escribir las mismas coordenadas otra vez no
            # tiene efecto, asi que se repite una a una para saber cual fallo
            for path in chunk:
                try:
                    _write_gps_exiftool(path, lat, lon, alt)
                    ok.append(path.name)
                except Exception as e:
                    errors.append({"file": path.name, "error": str(e)})
            continue
        for path in chunk:
            _after_write(path)
            _index_set_gps(path, lat, lon)
            ok.append(path.name)
    return jsonify({"ok": ok, "errors": errors})

@app.route("/api/gpsinfo")
def gpsinfo():
    rel = request.args.get("path", "")
    abs_path = _resolve_path(rel)
    if abs_path is None or not abs_path.exists():
        return jsonify({"has_gps": False, "error": "no existe"})
    # Si el indice ya sabe de esta foto y sigue al dia, no hace falta ExifTool
    known = _index_get(abs_path)
    if known is not None:
        if known["lat"] is None:
            return jsonify({"has_gps": False})
        return jsonify({"has_gps": True, "lat": str(known["lat"]), "lon": str(known["lon"])})
    try:
        result = subprocess.run(
            ["exiftool", "-fast2", "-json", "-n", "-q", "-q",
             "-GPSLatitude", "-GPSLongitude", str(abs_path)],
            capture_output=True, text=True, timeout=10
        )
        # Por NOMBRE, no por posicion: con "-s -s -s" un tag que falta no imprime
        # linea, y la longitud acababa leyendose como latitud
        fila = (json.loads(result.stdout or "[]") or [{}])[0]
        lat, lon = fila.get("GPSLatitude"), fila.get("GPSLongitude")
        if lat is None or lon is None:
            return jsonify({"has_gps": False})
        return jsonify({"has_gps": True, "lat": str(lat), "lon": str(lon)})
    except Exception as e:
        return jsonify({"has_gps": False, "error": str(e)})

@app.route("/api/dateinfo")
def dateinfo():
    """Fechas actuales de una foto (solo LECTURA)."""
    rel = request.args.get("path", "")
    abs_path = _resolve_path(rel)
    if abs_path is None or not abs_path.exists():
        return jsonify({"has_date": False, "error": "no existe"}), 404
    # Igual que con el GPS: si el indice lo sabe, nos ahorramos el proceso
    known = _index_get(abs_path)
    if known is not None and _clean_date(known["date"]):
        return jsonify({
            "date": _ui_date(known["date"]),
            "file_date": datetime.fromtimestamp(known["file_mtime"]).strftime(UI_DATE_FMT),
            "has_date": True,
        })
    info = _read_dates(abs_path)
    info["has_date"] = bool(info.get("date") or info.get("create_date"))
    return jsonify(info)

@app.route("/api/write_date", methods=["POST"])
def write_date():
    """
    Cambia la fecha de los metadatos de las fotos indicadas.
    mode = "set"   -> fecha absoluta (campo "date")
    mode = "shift" -> desplaza las fechas existentes (campo "shift")
    """
    data = request.json or {}
    files = data.get("files", [])
    if not files:
        return jsonify({"error": "sin archivos"}), 400
    sync_file = bool(data.get("sync_file", True))
    mode = data.get("mode", "set")

    exif_date, shift = None, None
    if mode == "shift":
        shift = _build_shift(data.get("shift") or {})
        if not shift:
            return jsonify({"error": "desplazamiento no valido"}), 400
    else:
        exif_date = _parse_user_datetime(data.get("date"))
        if not exif_date:
            return jsonify({"error": "fecha no valida"}), 400

    targets, ok, errors = [], [], []
    for rel in files:
        path = _resolve_path(rel)
        if path is None or not path.exists():
            errors.append({"file": rel, "error": "no existe o ruta invalida"})
            continue
        if path.suffix.lower() not in SUPPORTED_EXTS:
            errors.append({"file": path.name, "error": "formato no soportado"})
            continue
        try:
            targets.append(sanitize_path(path))
        except Exception as e:
            errors.append({"file": path.name, "error": str(e)})

    # Mismo cambio para todas: una sola llamada a ExifTool por lote
    if shift:
        op, val = shift
        args = ["-AllDates" + op + val]
        if sync_file:
            args.append("-FileModifyDate" + op + val)
    else:
        args = ["-AllDates=" + exif_date]
        if sync_file:
            args.append("-FileModifyDate=" + exif_date)

    def _done(path, fecha):
        _after_write(path)
        if fecha:
            _index_set_date(path, fecha)
        else:
            _index_stale(path)
        ok.append(path.name)

    # Un desplazamiento deja INTACTAS las fotos sin fecha previa, y ExifTool no
    # dice cuales fueron: hay que saberlo de antemano
    before = _read_dates_batch(targets) if shift else {}
    if shift:
        con_fecha = []
        for path in targets:
            previa = before.get(os.path.realpath(str(path))) or {}
            if previa.get("alguna"):
                con_fecha.append(path)
            else:
                errors.append({"file": path.name,
                               "error": "la foto no tiene fecha previa que desplazar"})
        targets = con_fecha

    for chunk in _chunks(targets, EXIFTOOL_CHUNK):
        try:
            result = _run_exiftool_batch(args, chunk)
        except Exception as e:
            for path in chunk:
                errors.append({"file": path.name, "error": str(e)})
            continue
        escritas = result.updated if result.returncode == 0 else 0
        if shift:
            # Un desplazamiento NO se puede repetir (desplazaria dos veces las
            # que si funcionaron), asi que en vez de reintentar se releen las
            # fechas del lote: dice cual cambio de verdad y ademas deja el
            # indice al dia, que es lo que evita releer foto a foto despues
            after = _read_dates_batch(chunk)
            for path in chunk:
                key = os.path.realpath(str(path))
                nueva = after.get(key) or {}
                previa = before.get(key) or {}
                if nueva.get("alguna") and nueva["alguna"] != previa.get("alguna"):
                    # Solo se anota en el indice la fecha de TOMA, que es la que
                    # usan la galeria y el renombrado
                    _done(path, nueva["toma"])
                else:
                    errors.append({"file": path.name,
                                   "error": "no se pudo desplazar la fecha"})
            continue
        if escritas == len(chunk):
            for path in chunk:
                _done(path, exif_date)
            continue
        # Poner una fecha concreta se puede repetir sin efectos: se reintenta una
        # a una para decir exactamente cual fallo
        for path in chunk:
            try:
                written = _write_date_exiftool(path, exif_date=exif_date,
                                               shift=shift, sync_file=sync_file)
                ok.append(written.name)
            except Exception as e:
                errors.append({"file": path.name, "error": str(e)})
    return jsonify({"ok": ok, "errors": errors, "date": exif_date or ""})

@app.route("/api/rotate", methods=["POST"])
def rotate_files():
    """Giro/volteo SIN PERDIDA: solo cambia el tag EXIF Orientation."""
    data = request.json or {}
    files = data.get("files", [])
    op = data.get("op", "")
    if not files:
        return jsonify({"error": "sin archivos"}), 400
    if op not in ORIENT_OPS:
        return jsonify({"error": "operacion no valida"}), 400
    ok, errors = [], []
    for rel in files:
        path = _resolve_path(rel)
        if path is None or not path.exists():
            errors.append({"file": rel, "error": "no existe o ruta invalida"})
            continue
        try:
            _rotate_lossless(path, op)
            ok.append(path.name)
        except Exception as e:
            errors.append({"file": path.name, "error": str(e)})
    return jsonify({"ok": ok, "errors": errors})

@app.route("/api/imginfo")
def imginfo():
    """Dimensiones de la foto tal como se ve (ya orientada). Solo LECTURA.

    El editor pide esto nada mas abrirse, asi que tiene que ser instantaneo: el
    ancho y el alto estan en la cabecera del archivo, no hace falta
    descomprimir un solo pixel. Antes se abria la foto entera para leer dos
    numeros, lo que en una de 48 Mpx eran 1,7 s y 380 MB de memoria cada vez
    que se pulsaba "Editar"."""
    rel = request.args.get("path", "")
    abs_path = _resolve_path(rel)
    if abs_path is None or not abs_path.exists():
        return jsonify({"error": "no existe"}), 404
    try:
        if abs_path.suffix.lower() in RAW_EXTS:
            # En un RAW lo editable es la vista previa incrustada, asi que es
            # SU tamano el que hay que informar.
            data = _extract_raw_preview(abs_path)
            if not data:
                return jsonify({"error": "no se pudo leer la vista previa del RAW"}), 500
            with Image.open(io.BytesIO(data)) as img:
                w, h = img.size
            if _read_orientation(abs_path) in SWAP_ORIENTATIONS:
                w, h = h, w
        else:
            with Image.open(str(abs_path)) as img:
                w, h = _oriented_size(img)
        return jsonify({"width": w, "height": h})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/auto_levels")
def auto_levels():
    """Ajustes sugeridos para una foto (no modifica nada)."""
    rel = request.args.get("path", "")
    abs_path = _resolve_path(rel)
    if abs_path is None or not abs_path.exists():
        return jsonify({"error": "no existe"}), 404
    try:
        # El histograma se mira sobre una version pequena, asi que la foto se
        # abre ya reducida en vez de descomprimirla entera para tirarla.
        with _user_decode():
            img = _open_for_edit(abs_path, (512, 512))
            return jsonify(_auto_levels(img))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/edit", methods=["POST"])
def edit_image():
    """
    Aplica giro/volteo, recorte y ajustes de luz y color.
    Si solo hay giro/volteo se usa la via SIN PERDIDA (tag Orientation).
    En cuanto hay recorte o ajustes hay que recodificar los pixeles: se
    sobrescribe el original (la interfaz avisa antes), salvo en RAW, que no
    admite reescritura y genera un JPEG derivado.
    """
    data = request.json or {}
    rel = data.get("path", "")
    abs_path = _resolve_path(rel)
    if abs_path is None or not abs_path.exists() or not abs_path.is_file():
        return jsonify({"error": "la foto no existe"}), 404

    rotate = str(data.get("rotate", "0"))
    flip_h = bool(data.get("flip_h"))
    flip_v = bool(data.get("flip_v"))
    crop = data.get("crop") or None
    adj = data.get("adj") or {}
    try:
        angle = max(-15.0, min(15.0, float(data.get("straighten") or 0)))
    except (TypeError, ValueError):
        angle = 0.0
    try:
        sharpen = max(-100.0, min(150.0, float(data.get("sharpen") or 0)))
    except (TypeError, ValueError):
        sharpen = 0.0
    has_adj = not _adj_is_neutral(adj) or angle != 0 or sharpen != 0

    # Via sin perdida: giro y/o volteo, nada mas
    if not crop and not has_adj:
        ops = []
        if rotate == "90":
            ops.append("cw")
        elif rotate == "180":
            ops.append("180")
        elif rotate == "270":
            ops.append("ccw")
        if flip_h:
            ops.append("fliph")
        if flip_v:
            ops.append("flipv")
        if not ops:
            return jsonify({"error": "no hay cambios que guardar"}), 400
        try:
            for op in ops:
                _rotate_lossless(abs_path, op)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True, "lossless": True, "path": rel, "name": abs_path.name})

    # Via con recodificacion. Aqui SI hace falta la foto entera (es lo que se
    # va a guardar), por eso pasa por el turno: dos ediciones simultaneas de
    # fotos grandes se comen la memoria del contenedor.
    try:
        with _user_decode():
            img = _open_for_edit(abs_path)
            if rotate == "90":
                img = img.transpose(Image.ROTATE_270)
            elif rotate == "180":
                img = img.transpose(Image.ROTATE_180)
            elif rotate == "270":
                img = img.transpose(Image.ROTATE_90)
            if flip_h:
                img = img.transpose(Image.FLIP_LEFT_RIGHT)
            if flip_v:
                img = img.transpose(Image.FLIP_TOP_BOTTOM)

            img = _straighten(img, angle)

            if crop:
                w, h = img.size
                x0 = int(round(max(0.0, min(1.0, float(crop.get("x", 0)))) * w))
                y0 = int(round(max(0.0, min(1.0, float(crop.get("y", 0)))) * h))
                x1 = x0 + int(round(max(0.0, min(1.0, float(crop.get("w", 1)))) * w))
                y1 = y0 + int(round(max(0.0, min(1.0, float(crop.get("h", 1)))) * h))
                x1 = min(w, max(x0 + 1, x1))
                y1 = min(h, max(y0 + 1, y1))
                if (x0, y0, x1, y1) != (0, 0, w, h):
                    img = img.crop((x0, y0, x1, y1))

            # La nitidez va ANTES de los ajustes tonales: asi el editor puede
            # cachear su resultado y los sliders de luz y color siguen costando
            # solo el LUT (ademas de ser el orden habitual, enfocar el escaneado y
            # graduarlo despues)
            img = _apply_sharpen(img, sharpen)
            img = _apply_adjustments(img, adj)
            dest, derived = _save_edit(img, abs_path)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    root = _work_root()
    return jsonify({
        "ok": True,
        "lossless": False,
        "derived": derived,
        "name": dest.name,
        "path": str(dest.relative_to(root)),
    })

def _index_status_dict():
    return {
        "scanning": bool(_index_state["scanning"]),
        "done": _index_state["done"],
        "total": _index_state["total"],
        "updated_at": _index_state["at"],
    }

@app.route("/api/mkdir", methods=["POST"])
def make_dir():
    """Crea una carpeta dentro de la carpeta de trabajo."""
    data = request.json or {}
    rel = (data.get("path") or "").strip()
    name = _safe_upload_name(data.get("name") or "")
    if not name:
        return jsonify({"error": "nombre no valido"}), 400
    parent = _resolve_path(rel)
    if parent is None or not parent.is_dir():
        return jsonify({"error": "la carpeta de destino no existe"}), 404
    target = parent / name
    if target.exists():
        return jsonify({"error": "ya existe una carpeta con ese nombre"}), 400
    try:
        target.mkdir()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    root = _work_root()
    return jsonify({"ok": True, "name": name, "path": str(target.relative_to(root))})

@app.route("/api/transfer", methods=["POST"])
def transfer_files():
    """
    Mueve o copia fotos a otra carpeta.
    Mover es una operacion de sistema de archivos; copiar duplica el archivo
    byte a byte (copy2 conserva ademas las fechas). En ningun caso se abre ni
    se recodifica la imagen.
    """
    data = request.json or {}
    files = data.get("files", [])
    mode = "copy" if data.get("mode") == "copy" else "move"
    dest = _resolve_path((data.get("dest") or "").strip())
    if dest is None or not dest.is_dir():
        return jsonify({"error": "la carpeta de destino no existe"}), 404
    if not files:
        return jsonify({"error": "sin archivos"}), 400

    ok, errors = [], []
    for rel in files:
        src = _resolve_path(rel)
        if src is None or not src.exists() or not src.is_file():
            errors.append({"file": rel, "error": "no existe o ruta invalida"})
            continue
        if src.parent == dest:
            errors.append({"file": src.name, "error": "ya esta en esa carpeta"})
            continue
        try:
            target = _unique_target(dest, src.name)
            if mode == "copy":
                shutil.copy2(str(src), str(target))
                _index_copy(src, target)
            else:
                shutil.move(str(src), str(target))
                _evict_thumb_cache(src)
                _index_move(src, target)
            _trigger_reindex(target)
            ok.append({"name": src.name, "new": target.name})
        except Exception as e:
            errors.append({"file": src.name, "error": str(e)})
    root = _work_root()
    return jsonify({"ok": ok, "errors": errors, "dest": str(dest.relative_to(root))})

@app.route("/api/gps_map")
def gps_map():
    """
    Coordenadas de las fotos de una carpeta (y sus subcarpetas) para el mapa.
    Se resuelve con UNA sola llamada a ExifTool sobre el arbol: lanzar un
    proceso por foto seria inviable con miles de archivos.
    """
    rel = request.args.get("path", "")
    base = _resolve_path(rel)
    if base is None or not base.is_dir():
        return jsonify({"error": "la carpeta no existe"}), 404
    root = _work_root()

    # Se responde con lo que el indice ya sabe (instantaneo) y se pide un
    # repaso en segundo plano: la interfaz va completando el mapa sola
    _index_request(root)
    photos = []
    for path, lat, lon, date, mtime in _index_photos(base):
        try:
            relative = Path(path).relative_to(root)
        except Exception:
            continue
        folder = str(relative.parent)
        photos.append({
            "path": str(relative),
            "name": os.path.basename(path),
            "folder": "" if folder == "." else folder,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "date": date or "",
            # La galeria ordena y filtra por fecha de archivo: asi un grupo del
            # mapa se puede abrir como un album sin volver a leer nada
            "mtime": mtime or 0,
            "ext": os.path.splitext(path)[1].lower(),
        })
    return jsonify({
        "photos": photos,
        "total": _index_count(base),
        "with_gps": len(photos),
        "index": _index_status_dict(),
    })

# Momento en que arranco este proceso: sirve para distinguir "se reconstruyo la
# imagen" de "solo se reinicio el contenedor".
ARRANQUE = time.time()

@app.route("/api/version")
def version():
    """Que version se esta ejecutando AHORA MISMO.

    Sin esto, comprobar que el NAS se ha actualizado de verdad es adivinar: el
    script puede decir "listo" habiendo fallado el pull, o el contenedor puede
    haberse reiniciado con la imagen vieja, y por fuera todo se ve igual. Aqui
    responde el propio proceso que esta atendiendo, asi que si contesta con el
    commit correcto es que ese codigo es el que corre."""
    return jsonify({
        "commit": os.environ.get("GEOTAGGER_COMMIT", "desconocido"),
        "branch": os.environ.get("GEOTAGGER_BRANCH", "desconocido"),
        "built": os.environ.get("GEOTAGGER_BUILD", "desconocido"),
        "started": datetime.fromtimestamp(ARRANQUE).strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_s": int(time.time() - ARRANQUE),
    })

@app.route("/api/index_status")
def index_status():
    """Como va el indice, para que la interfaz avise y se refresque sola."""
    return jsonify(_index_status_dict())

@app.route("/api/auto_edit", methods=["POST"])
def auto_edit_files():
    """
    Aplica el "Auto restaurar" a varias fotos de una vez: para cada una se
    calculan sus propios niveles por canal y su relleno de sombras. Recodifica
    y sobrescribe el original (la interfaz avisa antes); los RAW generan un
    JPEG derivado y quedan intactos.
    """
    data = request.json or {}
    files = data.get("files", [])
    if not files:
        return jsonify({"error": "sin archivos"}), 400
    try:
        sharpen = max(-100.0, min(150.0, float(data.get("sharpen") or 0)))
    except (TypeError, ValueError):
        sharpen = 0.0

    ok, errors = [], []
    root = _work_root()
    for rel in files:
        path = _resolve_path(rel)
        if path is None or not path.exists() or not path.is_file():
            errors.append({"file": rel, "error": "no existe o ruta invalida"})
            continue
        if path.suffix.lower() not in SUPPORTED_EXTS:
            errors.append({"file": path.name, "error": "formato no soportado"})
            continue
        try:
            with _user_decode():
                img = _open_for_edit(path)
                adj = _auto_levels(_downscaled(img, (512, 512)))
                img = _apply_sharpen(img, sharpen)
                img = _apply_adjustments(img, adj)
                dest, derived = _save_edit(img, path)
            ok.append({"name": path.name, "new": dest.name,
                       "path": str(dest.relative_to(root)), "derived": derived})
        except Exception as e:
            errors.append({"file": path.name, "error": str(e)})
    return jsonify({"ok": ok, "errors": errors})

@app.route("/api/missing_gps")
def missing_gps():
    rel = request.args.get("path", "")
    base = _resolve_path(rel)
    if base is None or not base.exists():
        return jsonify({"error": "no existe", "files": []}), 404
    root = _work_root()
    # Del indice: antes esto lanzaba un ExifTool POR FOTO
    _index_request(root)
    found = []
    for path, mtime, date in _index_without_gps(base):
        item = Path(path)
        if not item.exists():
            continue
        try:
            relative = item.relative_to(root)
        except Exception:
            continue
        folder = str(relative.parent)
        found.append({
            "name": item.name,
            "path": str(relative),
            "ext": item.suffix.lower(),
            "folder": "" if folder == "." else folder,
            "mtime": mtime if mtime and mtime > 0 else 0,
            "date": _ui_date(date),
        })
    found.sort(key=lambda f: f["mtime"], reverse=True)
    return jsonify({"files": found, "count": len(found), "index": _index_status_dict()})

@app.route("/api/rename", methods=["POST"])
def rename_files():
    """
    Renombra archivos. SOLO cambia el nombre (operacion de sistema de archivos
    via Path.rename). NO abre, NO lee el contenido de pixel, NO reescribe la
    imagen. El archivo es identico byte a byte tras el renombrado.
    """
    data = request.json
    fallback_name = data.get("location_name", "").strip()
    files = data["files"]
    ok, errors = [], []
    seen = {}
    geocode_cache = {}
    geo_down = False
    for rel in files:
        path = _resolve_path(rel)
        if path is None or not path.exists():
            errors.append({"file": rel, "error": "no existe o ruta invalida"})
            continue
        try:
            location_name = ""
            # El indice ya tiene coordenadas y fecha de casi todas las fotos:
            # antes se lanzaban DOS ExifTool por foto solo para leerlas
            known = _index_get(path)
            coords = None
            if known is not None:
                coords = (known["lat"], known["lon"]) if known["lat"] is not None else None
            else:
                coords = _get_coords(path)
            if coords:
                # El nombre del lugar se guarda para siempre: consultarlo cuesta
                # una peticion de red MAS un segundo de espera obligatoria, y
                # antes se repetia por cada coordenada distinta (a 11 m, casi
                # una por foto)
                cache_key = _place_key(coords[0], coords[1])
                name = geocode_cache.get(cache_key) or _place_cached(cache_key)
                if name is None and not geo_down:
                    rev = _reverse_geocode(coords[0], coords[1])
                    if rev:
                        name = rev
                        _place_store(cache_key, rev)
                        time.sleep(1)      # limite de uso de Nominatim
                    else:
                        geo_down = True    # sin red: no insistir con el resto
                if name:
                    location_name = name
                    geocode_cache[cache_key] = name
            if not location_name and fallback_name:
                location_name = fallback_name
            if not location_name:
                location_name = "sin-ubicacion"
            location_name = re.sub(r'[^\w\s-]', '', location_name).strip()
            location_name = re.sub(r'\s+', '_', location_name)
            if known is not None:
                dt_raw = known["date"]
            else:
                result = subprocess.run(
                    ["exiftool", "-fast2", "-DateTimeOriginal", "-s", "-s", "-s", str(path)],
                    capture_output=True, text=True, timeout=10
                )
                dt_raw = result.stdout.strip()
            # Un valor que no sea una fecha no puede acabar en el nombre
            dt_raw = _clean_date(dt_raw)
            if dt_raw:
                dt = dt_raw.replace(":", "-", 2).replace(" ", "_").replace(":", "-")
            else:
                dt = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            base_name = dt + "_" + location_name
            ext = path.suffix
            candidate = path.parent / (base_name + ext)
            counter = 2
            while candidate.exists() or str(candidate) in seen.values():
                candidate = path.parent / (base_name + "_" + str(counter) + ext)
                counter += 1
            seen[rel] = str(candidate)
            path.rename(candidate)
            # Invalidate old thumbnail cache
            _evict_thumb_cache(path)
            _index_move(path, candidate)
            ok.append({"old": path.name, "new": candidate.name})
        except Exception as e:
            errors.append({"file": path.name, "error": str(e)})
    return jsonify({"ok": ok, "errors": errors})

@app.route("/api/send_report", methods=["POST"])
def send_report():
    data = request.json
    action = data.get("action", "Operacion")
    folder = data.get("folder", "")
    ok_count = data.get("ok_count", 0)
    err_count = data.get("err_count", 0)
    total = data.get("total", 0)
    err_files = data.get("err_files", [])
    ts = datetime.now().strftime("%d/%m/%Y %H:%M")

    status = "OK" if err_count == 0 else "CON ERRORES"
    subject = "GeoTagger: " + action + " " + status + " (" + str(total) + " archivos)"

    body = "<div style='font-family:system-ui;max-width:500px;margin:0 auto;'>"
    body += "<h2 style='color:#1a73e8;'>GeoTagger — Reporte</h2>"
    body += "<table style='width:100%;border-collapse:collapse;'>"
    body += "<tr><td style='padding:8px;color:#888;'>Accion</td><td style='padding:8px;font-weight:600;'>" + action + "</td></tr>"
    body += "<tr><td style='padding:8px;color:#888;'>Carpeta</td><td style='padding:8px;'>" + (folder or "Raiz") + "</td></tr>"
    body += "<tr><td style='padding:8px;color:#888;'>Fecha</td><td style='padding:8px;'>" + ts + "</td></tr>"
    body += "<tr><td style='padding:8px;color:#888;'>Total</td><td style='padding:8px;'>" + str(total) + " archivos</td></tr>"
    body += "<tr><td style='padding:8px;color:#888;'>Exitosos</td><td style='padding:8px;color:#34a853;font-weight:600;'>" + str(ok_count) + "</td></tr>"
    body += "<tr><td style='padding:8px;color:#888;'>Errores</td><td style='padding:8px;color:" + ("#ea4335" if err_count > 0 else "#34a853") + ";font-weight:600;'>" + str(err_count) + "</td></tr>"
    body += "</table>"
    if err_files:
        body += "<h3 style='color:#ea4335;margin-top:16px;'>Archivos con error:</h3><ul>"
        for ef in err_files[:20]:
            body += "<li style='font-size:0.9em;'>" + ef + "</li>"
        if len(err_files) > 20:
            body += "<li>... y " + str(len(err_files) - 20) + " mas</li>"
        body += "</ul>"
    body += "</div>"

    try:
        _send_report(subject, body)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/delete", methods=["POST"])
def delete_files():
    """Elimina los archivos indicados de forma permanente."""
    data = request.json
    files = data.get("files", [])
    ok, errors = [], []
    for rel in files:
        path = _resolve_path(rel)
        if path is None:
            errors.append({"file": rel, "error": "ruta inválida"})
            continue
        if not path.exists() or not path.is_file():
            errors.append({"file": rel, "error": "no existe"})
            continue
        try:
            _evict_thumb_cache(path)
            path.unlink()
            _index_forget(path)
            ok.append(path.name)
        except Exception as e:
            errors.append({"file": path.name, "error": str(e)})
    return jsonify({"ok": ok, "errors": errors})

@app.route("/api/download")
def download_file():
    """Descarga el archivo original sin modificarlo."""
    rel = request.args.get("path", "")
    abs_path = _resolve_path(rel)
    if abs_path is None or not abs_path.exists() or not abs_path.is_file():
        return "", 404
    return send_file(str(abs_path), as_attachment=True, download_name=abs_path.name)

@app.route("/api/download_zip", methods=["POST"])
def download_zip():
    """
    Crea un ZIP con los archivos seleccionados y lo sirve como descarga.
    Usa ZIP_STORED (sin comprimir) porque los RAW ya están comprimidos;
    evita consumo innecesario de CPU en el NAS.

    El ZIP se monta EN DISCO. Armandolo en memoria, veinte fotos de 25 MB eran
    medio giga retenido en el contenedor hasta que el navegador terminara de
    bajarlo, que con una conexion lenta puede ser un buen rato.
    """
    data = request.json
    files = data.get("files", [])
    if not files:
        return jsonify({"error": "sin archivos"}), 400

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fd, tmp_path = tempfile.mkstemp(prefix="zip_", suffix=".zip", dir=TMP_DIR)
    os.close(fd)
    try:
        seen_names = {}
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED,
                             allowZip64=True) as zf:
            for rel in files:
                path = _resolve_path(rel)
                if not path or not path.exists() or not path.is_file():
                    continue
                name = path.name
                if name in seen_names:
                    seen_names[name] += 1
                    name = path.stem + "_" + str(seen_names[path.name]) + path.suffix
                else:
                    seen_names[path.name] = 1
                zf.write(str(path), name)
    except Exception as e:
        _discard(tmp_path)
        return jsonify({"error": str(e)}), 500

    # El temporal se borra AHORA, con la descarga todavia por delante: en Linux
    # el archivo sigue existiendo mientras alguien lo tenga abierto, asi que el
    # ZIP se sirve entero y el espacio se libera solo al terminar. Pase lo que
    # pase -que el navegador cancele, que se caiga el contenedor- no queda
    # basura en el disco del NAS.
    try:
        handle = open(tmp_path, "rb")
        size = os.fstat(handle.fileno()).st_size
    except OSError as e:
        _discard(tmp_path)
        return jsonify({"error": str(e)}), 500
    _discard(tmp_path)
    resp = send_file(
        handle,
        mimetype="application/zip",
        as_attachment=True,
        download_name="GeoTagger_" + ts + ".zip"
    )
    # Sin ruta que mirar, send_file no sabe cuanto ocupa y manda la respuesta
    # "a trozos", sin tamano: el navegador se queda sin barra de progreso y sin
    # tiempo estimado. Se lo decimos nosotros, que el descriptor sigue abierto.
    resp.content_length = size
    return resp

def _discard(path):
    """Borra un temporal sin quejarse si ya no esta."""
    try:
        os.unlink(str(path))
    except OSError:
        pass

def _clean_tmp_dir():
    """Temporales que quedaron de una descarga cortada o de un reinicio."""
    try:
        for f in Path(TMP_DIR).iterdir():
            if f.is_file():
                _discard(f)
    except Exception:
        pass

_clean_tmp_dir()

def _safe_upload_name(filename):
    """Limpia el nombre de un archivo subido.
    - Elimina cualquier componente de ruta (solo se queda con el nombre base).
    - Sustituye caracteres invalidos por '-'.
    - Evita nombres ocultos o reservados (que empiecen por '.' o '@').
    Devuelve None si el nombre resultante no es utilizable."""
    if not filename:
        return None
    # Descartar cualquier ruta que pudiera venir en el nombre (../, C:\, etc.)
    name = os.path.basename(filename.replace("\\", "/")).strip()
    name = re.sub(r'[:\*\?"<>\|]', '-', name)
    while name.startswith(".") or name.startswith("@"):
        name = name[1:]
    name = name.strip()
    if not name or name in (".", ".."):
        return None
    return name

@app.route("/api/upload", methods=["POST"])
def upload_files():
    """
    Guarda las fotos subidas en la carpeta de destino indicada.
    Escribe el stream recibido TAL CUAL en disco (file.save copia byte a byte);
    NO recomprime ni re-codifica la imagen, respetando el principio de la app.
    Si un nombre ya existe, se anade un sufijo numerico para no sobrescribir.
    """
    rel = request.form.get("path", "")
    dest = _resolve_path(rel)
    if dest is None:
        return jsonify({"error": "ruta invalida"}), 400
    if not dest.exists() or not dest.is_dir():
        return jsonify({"error": "la carpeta de destino no existe"}), 404

    uploaded = request.files.getlist("files")
    if not uploaded:
        return jsonify({"error": "sin archivos"}), 400

    ok, errors = [], []
    for f in uploaded:
        if not f or not f.filename:
            continue
        name = _safe_upload_name(f.filename)
        if not name:
            errors.append({"file": f.filename, "error": "nombre invalido"})
            continue
        ext = Path(name).suffix.lower()
        if ext not in UPLOAD_EXTS:
            errors.append({"file": name, "error": "formato no soportado"})
            continue
        stem = Path(name).stem
        candidate = dest / name
        counter = 2
        while candidate.exists():
            candidate = dest / (stem + "_" + str(counter) + ext)
            counter += 1
        try:
            f.save(str(candidate))
            _trigger_reindex(candidate)
            ok.append(candidate.name)
        except Exception as e:
            errors.append({"file": name, "error": str(e)})

    if ok:
        _index_request(_work_root(), force=True)   # las nuevas entran en el indice
    return jsonify({"ok": ok, "errors": errors})

if __name__ == "__main__":
    # threaded=True allows Flask to handle multiple thumbnail requests concurrently
    # instead of queuing them one-by-one (critical for gallery performance)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
