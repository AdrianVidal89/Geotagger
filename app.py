import os
import subprocess
import piexif
import re
import io
import time
from pathlib import Path
from flask import Flask, jsonify, request, render_template, send_file
from PIL import Image
import requests

app = Flask(__name__)

PHOTOS_BASE = "/photos"
JPG_EXTS = {".jpg", ".jpeg", ".tiff", ".tif"}
RAW_EXTS = {".cr3", ".jpr", ".cr2", ".nef", ".arw", ".raf", ".dng"}

def sanitize_path(path):
    p = Path(path)
    clean_name = re.sub(r'[:\*\?"<>\|]', '-', p.name)
    if clean_name != p.name:
        new_path = p.parent / clean_name
        p.rename(new_path)
        return new_path
    return p

def _trigger_reindex(path):
    """Renombra el archivo a temporal y de vuelta para generar un evento
    de sistema de archivos que QuMagie detecte y reindexe solo esta foto."""
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

def decimal_to_dms(decimal):
    decimal = abs(decimal)
    degrees = int(decimal)
    minutes = int((decimal - degrees) * 60)
    seconds = round(((decimal - degrees) * 60 - minutes) * 60 * 10000)
    return ((degrees, 1), (minutes, 1), (seconds, 10000))

def build_gps_ifd(lat, lon, alt=None):
    gps = {
        piexif.GPSIFD.GPSLatitudeRef:  b"N" if lat >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude:     decimal_to_dms(lat),
        piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude:    decimal_to_dms(lon),
    }
    if alt is not None:
        gps[piexif.GPSIFD.GPSAltitudeRef] = 0 if alt >= 0 else 1
        gps[piexif.GPSIFD.GPSAltitude]    = (int(abs(alt) * 100), 100)
    return gps

def write_jpg_gps(path, lat, lon, alt=None):
    path = sanitize_path(path)
    img = Image.open(str(path))
    exif_bytes = img.info.get("exif", b"")
    exif_dict = piexif.load(exif_bytes) if exif_bytes else {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
    exif_dict["GPS"] = build_gps_ifd(lat, lon, alt)
    img.save(str(path), exif=piexif.dump(exif_dict))
    _trigger_reindex(path)

def write_raw_gps(path, lat, lon, alt=None):
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
            if path.suffix.lower() in JPG_EXTS:
                write_jpg_gps(path, lat, lon, alt)
            elif path.suffix.lower() in RAW_EXTS:
                write_raw_gps(path, lat, lon, alt)
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


def _has_gps_fast(path):
    """Devuelve True si el archivo ya tiene coordenadas GPS."""
    ext = path.suffix.lower()
    try:
        if ext in JPG_EXTS:
            img = Image.open(str(path))
            exif_bytes = img.info.get("exif", b"")
            if not exif_bytes:
                return False
            exif_dict = piexif.load(exif_bytes)
            gps = exif_dict.get("GPS", {})
            return piexif.GPSIFD.GPSLatitude in gps and piexif.GPSIFD.GPSLongitude in gps
        else:
            result = subprocess.run(
                ["exiftool", "-n", "-GPSLatitude", "-GPSLongitude", "-s", "-s", "-s", str(path)],
                capture_output=True, text=True, timeout=10
            )
            out = result.stdout.strip()
            return bool(out) and len(out.split("\n")) >= 2
    except Exception:
        return False

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
