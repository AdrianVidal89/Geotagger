//! Revelado a resolucion completa EN EL NAS, con el mismo motor que el editor
//! del navegador. El movil solo manda los ajustes: revelar una foto de 24 Mpx
//! en coma flotante pide mas de 1 GB, y Safari mataba el editor al guardar.
//!
//! Uso: lc-export ORIGINAL SALIDA ORIENTACION CALIDAD PNG(0|1) AJUSTES.json
//!                [TIPO ANCHO ALTO MASCARA.raw]...
//! AJUSTES.json es el estado del editor tal cual (lo mismo que `apply()` de
//! static/lightcraft/worker.js lleva al motor). Las mascaras de la IA (sujeto,
//! cielo) llegan ya calculadas: alfa de 8 bits, ANCHO x ALTO.
use std::{env, fs, process};

use geotagger_lightcraft::Editor;
use serde_json::Value;

fn die(msg: &str) -> ! {
    eprintln!("{msg}");
    process::exit(1)
}

/// Lo mismo que `apply()` del worker, paso a paso y en el mismo orden.
fn apply(e: &mut Editor, st: &Value) {
    e.reset();
    e.set_profile(st["profile"].as_str().filter(|s| !s.is_empty()).unwrap_or("lc.color"), 100.0);
    if let Some(values) = st["values"].as_object() {
        for (id, v) in values {
            if let Some(f) = v.as_f64() {
                e.set(id, f);
            }
        }
    }
    e.set_bw(st["bw"].as_bool().unwrap_or(false));
    e.set_orientation(st["orient"].as_u64().filter(|o| *o >= 1).unwrap_or(1) as u16);
    let c: Vec<f64> = st["crop"]
        .as_array()
        .map(|a| a.iter().map(|v| v.as_f64().unwrap_or(0.0)).collect())
        .filter(|v: &Vec<f64>| v.len() == 4)
        .unwrap_or_else(|| vec![0.0, 0.0, 1.0, 1.0]);
    e.set_crop(c[0], c[1], c[2], c[3], st["angle"].as_f64().unwrap_or(0.0));
    for ch in ["master", "red", "green", "blue"] {
        let xy: Vec<f64> = st["curves"][ch]
            .as_array()
            .map(|pts| pts.iter().flat_map(|p| p.as_array().cloned().unwrap_or_default()).filter_map(|v| v.as_f64()).collect())
            .unwrap_or_default();
        e.set_curve(ch, &xy);
    }
    let json = |v: &Value| if v.is_array() { v.to_string() } else { "[]".to_string() };
    if e.set_masks_json(&json(&st["masks"])).is_err() {
        die("mascaras no validas");
    }
    if e.set_spots_json(&json(&st["spots"])).is_err() {
        die("correcciones no validas");
    }
}

fn main() {
    let a: Vec<String> = env::args().collect();
    if a.len() < 7 || (a.len() - 7) % 4 != 0 {
        die("uso: lc-export ORIGINAL SALIDA ORIENTACION CALIDAD PNG AJUSTES.json [TIPO ANCHO ALTO MASCARA]...");
    }
    let orient: u16 = a[3].parse().unwrap_or(1);
    let quality: u8 = a[4].parse().unwrap_or(95);
    let png = a[5] == "1";
    let st: Value = fs::read(&a[6])
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_else(|| die("ajustes no validos"));
    let mut ed = {
        let bytes = fs::read(&a[1]).unwrap_or_else(|e| die(&format!("no se pudo leer la foto: {e}")));
        Editor::new(&bytes, orient, 0).unwrap_or_else(|_| die("no se pudo decodificar la foto"))
    }; // los bytes del original se sueltan aqui: solo queda la foto decodificada
    for m in a[7..].chunks(4) {
        let (w, h) = (m[1].parse().unwrap_or(0), m[2].parse().unwrap_or(0));
        let data = fs::read(&m[3]).unwrap_or_default();
        ed.set_matte(&m[0], w, h, &data);
    }
    apply(&mut ed, &st);
    let out = ed.export(png, quality).unwrap_or_else(|_| die("no se pudo codificar el resultado"));
    if let Err(e) = fs::write(&a[2], out) {
        die(&format!("no se pudo escribir el resultado: {e}"));
    }
}
