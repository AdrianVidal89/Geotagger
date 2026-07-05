import os
import subprocess
import re
import io
import time
import json
import hashlib
import zipfile
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from flask import Flask, jsonify, request, render_template, send_file
from PIL import Image, ImageOps
import requests
from datetime import datetime

app = Flask(__name__)

PHOTOS_BASE = "/photos"
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

def _resolve_path(rel, base=PHOTOS_BASE):
    """Safely resolve a user-supplied relative path under base.
    Returns None if the resolved path escapes the base directory."""
    base_p = Path(base).resolve()
    try:
        target = (base_p / rel).resolve()
    except Exception:
        return None
    if not str(target).startswith(str(base_p) + os.sep) and str(target) != str(base_p):
        return None
    return target

def _load_settings():
    try:
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"recipient_email": ""}

def _save_settings(data):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f)

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

@app.route("/api/missing_gps")
def missing_gps():
    rel = request.args.get("path", "")
    base = _resolve_path(rel)
    if base is None or not base.exists():
        return jsonify({"error": "no existe", "files": []}), 404
    found = []
    for item in sorted(base.rglob("*")):
        if item.is_dir():
            continue
        if any(part.startswith("@") or part.startswith(".") for part in item.relative_to(PHOTOS_BASE).parts):
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
                "path": str(item.relative_to(PHOTOS_BASE)),
                "ext": item.suffix.lower(),
                "folder": str(item.parent.relative_to(PHOTOS_BASE)),
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
