import os
import subprocess
import re
import io
import time
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from flask import Flask, jsonify, request, render_template, send_file
from PIL import Image
import requests
from datetime import datetime

app = Flask(__name__)

PHOTOS_BASE = "/photos"
SETTINGS_DIR = "/app/data"
SETTINGS_FILE = SETTINGS_DIR + "/settings.json"
os.makedirs(SETTINGS_DIR, exist_ok=True)
JPG_EXTS = {".jpg", ".jpeg", ".tiff", ".tif"}
RAW_EXTS = {".cr3", ".jpr", ".cr2", ".nef", ".arw", ".raf", ".dng"}

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.environ.get("SMTP_USER", "ecostruxureatlas@gmail.com")
SMTP_PASS = os.environ.get("SMTP_PASS", "ezpv lhfx qjer fxkm")

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
    if not recipient:
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
    byte a byte. NO recomprime ni reduce la calidad. Valido para JPG, TIFF,
    CR3 y cualquier RAW soportado.
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
    abs_path = Path(PHOTOS_BASE) / rel
    if not abs_path.exists():
        return jsonify({"error": "Ruta no existe"}), 404
    dirs, files = [], []
    for item in sorted(abs_path.iterdir()):
        if item.name.startswith("@") or item.name.startswith("."):
            continue
        if item.is_dir():
            dirs.append({"name": item.name, "path": str(Path(rel) / item.name)})
        elif item.suffix.lower() in JPG_EXTS | RAW_EXTS:
            files.append({"name": item.name, "path": str(Path(rel) / item.name), "ext": item.suffix.lower()})
    return jsonify({"dirs": dirs, "files": files, "current": rel})

@app.route("/api/thumb")
def thumb():
    """
    Genera una miniatura EN MEMORIA para mostrar en la interfaz.
    Para RAW extrae la miniatura embebida (ExifTool). Para JPG genera una
    copia reducida en un buffer. NUNCA escribe sobre el archivo original.
    """
    rel = request.args.get("path", "")
    abs_path = Path(PHOTOS_BASE) / rel
    if not abs_path.exists():
        return "", 404
    try:
        ext = abs_path.suffix.lower()
        if ext in RAW_EXTS:
            result = subprocess.run(
                ["exiftool", "-b", "-ThumbnailImage", str(abs_path)],
                capture_output=True, timeout=10
            )
            if result.returncode == 0 and result.stdout:
                return send_file(io.BytesIO(result.stdout), mimetype="image/jpeg")
        img = Image.open(str(abs_path))
        img.thumbnail((200, 200))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")   # buffer en memoria, NO el archivo original
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg")
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
        path = Path(PHOTOS_BASE) / rel
        try:
            ext = path.suffix.lower()
            if ext in JPG_EXTS or ext in RAW_EXTS:
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
    abs_path = Path(PHOTOS_BASE) / rel
    if not abs_path.exists():
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
    base = Path(PHOTOS_BASE) / rel
    if not base.exists():
        return jsonify({"error": "no existe", "files": []}), 404
    found = []
    for item in sorted(base.rglob("*")):
        if item.is_dir():
            continue
        if any(part.startswith("@") or part.startswith(".") for part in item.relative_to(PHOTOS_BASE).parts):
            continue
        if item.suffix.lower() not in (JPG_EXTS | RAW_EXTS):
            continue
        if not _has_gps_fast(item):
            found.append({
                "name": item.name,
                "path": str(item.relative_to(PHOTOS_BASE)),
                "ext": item.suffix.lower(),
                "folder": str(item.parent.relative_to(PHOTOS_BASE))
            })
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
        path = Path(PHOTOS_BASE) / rel
        if not path.exists():
            errors.append({"file": rel, "error": "no existe"})
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
            path.rename(candidate)   # solo cambia el nombre, contenido intacto
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
    body += "<h2 style='color:#e94560;'>GeoTagger — Reporte</h2>"
    body += "<table style='width:100%;border-collapse:collapse;'>"
    body += "<tr><td style='padding:8px;color:#888;'>Accion</td><td style='padding:8px;font-weight:600;'>" + action + "</td></tr>"
    body += "<tr><td style='padding:8px;color:#888;'>Carpeta</td><td style='padding:8px;'>" + (folder or "Raiz") + "</td></tr>"
    body += "<tr><td style='padding:8px;color:#888;'>Fecha</td><td style='padding:8px;'>" + ts + "</td></tr>"
    body += "<tr><td style='padding:8px;color:#888;'>Total</td><td style='padding:8px;'>" + str(total) + " archivos</td></tr>"
    body += "<tr><td style='padding:8px;color:#888;'>Exitosos</td><td style='padding:8px;color:#4caf82;font-weight:600;'>" + str(ok_count) + "</td></tr>"
    body += "<tr><td style='padding:8px;color:#888;'>Errores</td><td style='padding:8px;color:" + ("#e94560" if err_count > 0 else "#4caf82") + ";font-weight:600;'>" + str(err_count) + "</td></tr>"
    body += "</table>"
    if err_files:
        body += "<h3 style='color:#e94560;margin-top:16px;'>Archivos con error:</h3><ul>"
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
