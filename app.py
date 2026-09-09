import os
import math
import subprocess
import re
import io
import time
import json
import hashlib
import shutil
import zipfile
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from flask import Flask, jsonify, request, render_template, send_file
from PIL import Image, ImageOps, ImageChops, ImageFilter
import requests
from datetime import datetime

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
os.makedirs(SETTINGS_DIR, exist_ok=True)
os.makedirs(THUMB_CACHE_DIR, exist_ok=True)

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

def _evict_thumb_cache(abs_path):
    """Remove all cached thumbnails (JPEG + WebP) for a given file."""
    try:
        # Both formats share the same path prefix so glob both extensions
        base_key = hashlib.md5(str(abs_path).encode()).hexdigest()[:16]
        cache_dir = Path(THUMB_CACHE_DIR)
        for ext in ("*.jpg", "*.webp"):
            for f in cache_dir.glob(ext):
                if f.stem.startswith(base_key[:8]):
                    f.unlink(missing_ok=True)
    except Exception:
        pass

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
        if isinstance(val, str) and val.strip() and not val.startswith("0000"):
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
    if "0 image files updated" in (result.stdout or ""):
        if shift:
            raise Exception("la foto no tiene fecha previa que desplazar")
        raise Exception(result.stderr.strip().splitlines()[0] if result.stderr.strip()
                        else "no se pudo escribir la fecha")
    _trigger_reindex(path)
    _evict_thumb_cache(path)
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
    if result.returncode != 0 or "0 image files updated" in (result.stdout or ""):
        raise Exception(result.stderr.strip().splitlines()[0] if result.stderr.strip()
                        else "no se pudo girar")
    _trigger_reindex(path)
    _evict_thumb_cache(path)
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
    small = img.copy()
    small.thumbnail((256, 256))
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
    if a > 0:
        return ImageChops.subtract(ImageChops.add(img, hi), lo)
    return ImageChops.add(ImageChops.subtract(img, hi), lo)

def _open_for_edit(abs_path):
    """Abre la imagen ya orientada (como se ve en el visor). Para un RAW usa la
    vista previa incrustada, que es lo unico editable de ese formato."""
    ext = abs_path.suffix.lower()
    if ext in RAW_EXTS:
        data = _extract_raw_preview(abs_path)
        if not data:
            raise Exception("no se pudo leer la vista previa del RAW")
        img = Image.open(io.BytesIO(data))
        op = ORIENTATION_OPS.get(_read_orientation(abs_path))
        if op is not None:
            img = img.transpose(op)
        return img
    img = Image.open(str(abs_path))
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img

def _save_pixels(img, dest, ext):
    """Guarda la imagen en el formato que corresponde a la extension."""
    if ext in (".jpg", ".jpeg"):
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(str(dest), format="JPEG", quality=95, subsampling=0, optimize=True)
    elif ext == ".png":
        img.save(str(dest), format="PNG", optimize=True)
    elif ext == ".webp":
        img.save(str(dest), format="WEBP", quality=95, method=4)
    elif ext in (".tif", ".tiff"):
        # LZW es sin perdida y evita TIFF enormes sin comprimir
        img.save(str(dest), format="TIFF", compression="tiff_lzw")
    elif ext in HEIC_EXTS:
        img.save(str(dest), format="HEIF", quality=95)
    else:
        raise Exception("formato no editable")

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

def _save_edit(img, abs_path):
    """Guarda una imagen editada: escribe primero un temporal, le copia los
    metadatos del original y solo entonces reemplaza el archivo. Asi un fallo a
    medias nunca deja el original corrupto. Devuelve (destino, es_derivado)."""
    dest, derived = _edit_destination(abs_path)
    tmp = dest.with_name("." + dest.name + ".edit_tmp" + dest.suffix)
    try:
        _save_pixels(img, tmp, dest.suffix.lower())
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
    """Generate a stable cache filename based on file path + mtime + format."""
    try:
        mtime = str(int(os.path.getmtime(abs_path) * 1000))
    except Exception:
        mtime = "0"
    key = hashlib.md5((str(abs_path) + mtime + fmt).encode()).hexdigest()
    ext = ".webp" if "WEBP" in fmt else ".jpg"
    return Path(THUMB_CACHE_DIR) / (key + ext)

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
    return jsonify(_work_root_info())

@app.route("/api/browse")
def browse():
    rel = request.args.get("path", "")
    abs_path = _resolve_path(rel)
    if abs_path is None or not abs_path.exists():
        return jsonify({"error": "Ruta no existe"}), 404
    dirs, files = [], []
    for item in sorted(abs_path.iterdir()):
        if item.name.startswith("@") or item.name.startswith("."):
            continue
        if item.is_dir():
            dirs.append({"name": item.name, "path": str(Path(rel) / item.name)})
        elif item.suffix.lower() in SUPPORTED_EXTS:
            try:
                mtime = int(item.stat().st_mtime)
            except Exception:
                mtime = 0
            files.append({
                "name": item.name,
                "path": str(Path(rel) / item.name),
                "ext": item.suffix.lower(),
                "mtime": mtime,
            })
    # Las fotos mas recientes primero (por fecha de modificacion del archivo).
    files.sort(key=lambda f: f["mtime"], reverse=True)
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
        etag = cache_file.stem
        if request.headers.get("If-None-Match") == etag:
            return "", 304
        resp = send_file(str(cache_file), mimetype=mime)
        resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
        resp.headers["ETag"] = etag
        resp.headers["Vary"] = "Accept"
        return resp

    try:
        ext = abs_path.suffix.lower()
        if ext in RAW_EXTS:
            result = subprocess.run(
                ["exiftool", "-b", "-ThumbnailImage", str(abs_path)],
                capture_output=True, timeout=15
            )
            if result.returncode == 0 and result.stdout:
                if fmt == "WEBP":
                    # Convert embedded JPEG thumbnail to WebP
                    img = Image.open(io.BytesIO(result.stdout))
                    buf = io.BytesIO()
                    img.save(buf, format="WEBP", quality=82, method=4)
                    cache_file.write_bytes(buf.getvalue())
                else:
                    cache_file.write_bytes(result.stdout)
                resp = send_file(str(cache_file), mimetype=mime)
                resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
                resp.headers["ETag"] = cache_file.stem
                resp.headers["Vary"] = "Accept"
                return resp

        img = Image.open(str(abs_path))
        img.thumbnail((300, 300))
        # Preserve EXIF orientation without reloading metadata
        if hasattr(img, '_getexif'):
            try:
                from PIL import ImageOps
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass
        buf = io.BytesIO()
        if fmt == "WEBP":
            img.save(buf, format="WEBP", quality=82, method=4)
        else:
            # JPEG no admite canal alfa (PNG/WebP RGBA) ni paleta
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=82, optimize=True)
        data = buf.getvalue()
        cache_file.write_bytes(data)
        resp = send_file(io.BytesIO(data), mimetype=mime)
        resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
        resp.headers["ETag"] = cache_file.stem
        resp.headers["Vary"] = "Accept"
        return resp
    except Exception as e:
        return str(e), 404

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
        ext = abs_path.suffix.lower()
        img = None
        if ext in RAW_EXTS:
            data = _extract_raw_preview(abs_path)
            if data:
                img = Image.open(io.BytesIO(data))
                # Los previews embebidos no llevan EXIF: aplicar la
                # orientacion declarada en el RAW original.
                op = ORIENTATION_OPS.get(_read_orientation(abs_path))
                if op is not None:
                    img = img.transpose(op)
        if img is None:
            img = Image.open(str(abs_path))
            try:
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass
        img.thumbnail((PREVIEW_MAX, PREVIEW_MAX))
        if fmt == "JPEG" and img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        if fmt == "WEBP":
            img.save(buf, format="WEBP", quality=88, method=4)
        else:
            img.save(buf, format="JPEG", quality=88, optimize=True)
        cache_file.write_bytes(buf.getvalue())
        return _cached_image_response(cache_file, mime)
    except Exception as e:
        return str(e), 404

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
    data = request.json
    lat   = float(data["lat"])
    lon   = float(data["lon"])
    alt   = float(data["alt"]) if data.get("alt") else None
    files = data["files"]
    ok, errors = [], []
    for rel in files:
        path = _resolve_path(rel)
        if path is None:
            errors.append({"file": rel, "error": "ruta invalida"})
            continue
        try:
            ext = path.suffix.lower()
            if ext in SUPPORTED_EXTS:
                _write_gps_exiftool(path, lat, lon, alt)
                ok.append(path.name)
            else:
                errors.append({"file": path.name, "error": "formato no soportado"})
        except Exception as e:
            errors.append({"file": path.name, "error": str(e)})
    return jsonify({"ok": ok, "errors": errors})

@app.route("/api/gpsinfo")
def gpsinfo():
    rel = request.args.get("path", "")
    abs_path = _resolve_path(rel)
    if abs_path is None or not abs_path.exists():
        return jsonify({"has_gps": False, "error": "no existe"})
    try:
        result = subprocess.run(
            ["exiftool", "-n", "-GPSLatitude", "-GPSLongitude", "-s", "-s", "-s", str(abs_path)],
            capture_output=True, text=True, timeout=10
        )
        out = result.stdout.strip()
        if out and "\n" in out:
            parts = out.split("\n")
            return jsonify({"has_gps": True, "lat": parts[0].strip(), "lon": parts[1].strip()})
        return jsonify({"has_gps": False})
    except Exception as e:
        return jsonify({"has_gps": False, "error": str(e)})

@app.route("/api/dateinfo")
def dateinfo():
    """Fechas actuales de una foto (solo LECTURA)."""
    rel = request.args.get("path", "")
    abs_path = _resolve_path(rel)
    if abs_path is None or not abs_path.exists():
        return jsonify({"has_date": False, "error": "no existe"}), 404
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

    ok, errors = [], []
    for rel in files:
        path = _resolve_path(rel)
        if path is None or not path.exists():
            errors.append({"file": rel, "error": "no existe o ruta invalida"})
            continue
        if path.suffix.lower() not in SUPPORTED_EXTS:
            errors.append({"file": path.name, "error": "formato no soportado"})
            continue
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
    """Dimensiones de la foto tal como se ve (ya orientada). Solo LECTURA."""
    rel = request.args.get("path", "")
    abs_path = _resolve_path(rel)
    if abs_path is None or not abs_path.exists():
        return jsonify({"error": "no existe"}), 404
    try:
        img = _open_for_edit(abs_path)
        return jsonify({"width": img.size[0], "height": img.size[1]})
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
        img = _open_for_edit(abs_path)
        img.thumbnail((512, 512))
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

    # Via con recodificacion
    try:
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
            else:
                shutil.move(str(src), str(target))
                _evict_thumb_cache(src)
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
    cmd = ["exiftool", "-json", "-n", "-q", "-q",
           "-GPSLatitude", "-GPSLongitude", "-DateTimeOriginal", "-SourceFile"]
    for ext in sorted(SUPPORTED_EXTS):
        cmd += ["-ext", ext.lstrip(".")]
    cmd += ["-r", str(base)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        items = json.loads(result.stdout) if result.stdout.strip() else []
    except Exception as e:
        return jsonify({"error": str(e), "photos": []}), 500

    photos, total = [], 0
    for item in items:
        try:
            path = Path(item.get("SourceFile", "")).resolve()
            relative = path.relative_to(root)
        except Exception:
            continue
        if any(part.startswith("@") or part.startswith(".") for part in relative.parts):
            continue
        total += 1
        lat, lon = item.get("GPSLatitude"), item.get("GPSLongitude")
        if lat is None or lon is None:
            continue
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        folder = str(relative.parent)
        photos.append({
            "path": str(relative),
            "name": path.name,
            "folder": "" if folder == "." else folder,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "date": item.get("DateTimeOriginal") or "",
        })
    return jsonify({"photos": photos, "total": total, "with_gps": len(photos)})

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
            img = _open_for_edit(path)
            sample = img.copy()
            sample.thumbnail((512, 512))
            adj = _auto_levels(sample)
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
    found = []
    for item in sorted(base.rglob("*")):
        if item.is_dir():
            continue
        if any(part.startswith("@") or part.startswith(".") for part in item.relative_to(root).parts):
            continue
        if item.suffix.lower() not in SUPPORTED_EXTS:
            continue
        if not _has_gps_fast(item):
            try:
                mtime = int(item.stat().st_mtime)
            except Exception:
                mtime = 0
            found.append({
                "name": item.name,
                "path": str(item.relative_to(root)),
                "ext": item.suffix.lower(),
                "folder": str(item.parent.relative_to(root)),
                "mtime": mtime,
            })
    found.sort(key=lambda f: f["mtime"], reverse=True)
    return jsonify({"files": found, "count": len(found)})

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
    for rel in files:
        path = _resolve_path(rel)
        if path is None or not path.exists():
            errors.append({"file": rel, "error": "no existe o ruta invalida"})
            continue
        try:
            location_name = ""
            coords = _get_coords(path)
            if coords:
                cache_key = (round(coords[0], 4), round(coords[1], 4))
                if cache_key in geocode_cache:
                    location_name = geocode_cache[cache_key]
                else:
                    rev = _reverse_geocode(coords[0], coords[1])
                    if rev:
                        location_name = rev
                        geocode_cache[cache_key] = rev
                        time.sleep(1)
            if not location_name and fallback_name:
                location_name = fallback_name
            if not location_name:
                location_name = "sin-ubicacion"
            location_name = re.sub(r'[^\w\s-]', '', location_name).strip()
            location_name = re.sub(r'\s+', '_', location_name)
            result = subprocess.run(
                ["exiftool", "-DateTimeOriginal", "-s", "-s", "-s", str(path)],
                capture_output=True, text=True, timeout=10
            )
            dt_raw = result.stdout.strip()
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
    """
    data = request.json
    files = data.get("files", [])
    if not files:
        return jsonify({"error": "sin archivos"}), 400

    buf = io.BytesIO()
    seen_names = {}
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
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
    buf.seek(0)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name="GeoTagger_" + ts + ".zip"
    )

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

    return jsonify({"ok": ok, "errors": errors})

if __name__ == "__main__":
    # threaded=True allows Flask to handle multiple thumbnail requests concurrently
    # instead of queuing them one-by-one (critical for gallery performance)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
