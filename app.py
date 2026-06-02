import os
import subprocess
import re
import io
import time
from pathlib import Path
from flask import Flask, jsonify, request, render_template, send_file
from PIL import Image
import requests
from datetime import datetime

app = Flask(__name__)

PHOTOS_BASE = "/photos"
JPG_EXTS = {".jpg", ".jpeg", ".tiff", ".tif"}
RAW_EXTS = {".cr3", ".jpr", ".cr2", ".nef", ".arw", ".raf", ".dng"}

# Config PhotoPrism (via API HTTP, no docker)
PHOTOPRISM_URL = os.environ.get("PHOTOPRISM_URL", "http://172.29.20.2:2342")
PHOTOPRISM_USER = os.environ.get("PHOTOPRISM_USER", "admin")
PHOTOPRISM_PASS = os.environ.get("PHOTOPRISM_PASS", "cambiame123")

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

def write_gps_exiftool(path, lat, lon, alt=None):
    """
    Escribe GPS usando SOLO exiftool — no recomprime la imagen.
    Funciona para JPG, TIFF y todos los RAW.
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
        str(path)
    ]
    if alt is not None:
        cmd.insert(-1, "-GPSAltitude=" + str(abs(alt)))
        cmd.insert(-1, "-GPSAltitudeRef=" + ("0" if alt >= 0 else "1"))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(result.stderr.strip())
    _trigger_reindex(path)

def _has_gps_fast(path):
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
    """Lee las coordenadas GPS del archivo. Devuelve (lat, lon) o None."""
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
    """Obtiene un nombre de lugar corto a partir de coordenadas."""
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
        img.save(buf, format="JPEG")
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
            # exiftool para todo — JPG y RAW — sin recomprimir nunca
            write_gps_exiftool(path, lat, lon, alt)
            ok.append(path.name)
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
            path.rename(candidate)
            ok.append({"old": path.name, "new": candidate.name})
        except Exception as e:
            errors.append({"file": path.name, "error": str(e)})
    return jsonify({"ok": ok, "errors": errors})

@app.route("/api/photoprism_index", methods=["POST"])
def photoprism_index():
    """Lanza reindexado en PhotoPrism via su API HTTP."""
    try:
        login = requests.post(
            PHOTOPRISM_URL + "/api/v1/session",
            json={"username": PHOTOPRISM_USER, "password": PHOTOPRISM_PASS},
            timeout=15
        )
        if login.status_code != 200:
            return jsonify({"ok": False, "error": "Login PhotoPrism fallo (" + str(login.status_code) + ")"})
        token = login.headers.get("X-Session-ID") or login.json().get("id")
        if not token:
            return jsonify({"ok": False, "error": "No se obtuvo token de sesion"})
        idx = requests.post(
            PHOTOPRISM_URL + "/api/v1/index",
            json={"path": "/", "rescan": False, "cleanup": True},
            headers={"X-Session-ID": token},
            timeout=30
        )
        if idx.status_code in (200, 201):
            return jsonify({"ok": True, "output": "Indexado lanzado en PhotoPrism"})
        else:
            return jsonify({"ok": False, "error": "Index fallo (" + str(idx.status_code) + ")"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
