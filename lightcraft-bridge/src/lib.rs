//! Puente WebAssembly entre el editor de Geotagger y el motor de revelado de
//! LightCraft (`lightcraft-pipeline`).
//!
//! El navegador le pasa los bytes de la foto (JPEG, PNG, WebP o TIFF), el
//! puente la decodifica a luz lineal Rec.2020 con los codecs de LightCraft y
//! la revela con su pipeline (32 bits en coma flotante, el mismo de su app de
//! escritorio). Los ajustes se tocan por id de control (`light.exposure`,
//! `color.vibrance`, ...), la misma tabla que usa la interfaz de LightCraft,
//! asi que rangos y valores por defecto son los suyos.
//!
//! Nada de esto corre en el NAS: el revelado ocupa la CPU del equipo que edita
//! y al servidor solo le llega el JPEG terminado.
#![forbid(unsafe_code)]

use std::sync::Arc;

use lightcraft_codecs::{ChromaSubsampling, DecodeOptions, EncodeImage, EncodeMeta};
use lightcraft_develop::{DevelopSettings, Treatment, WbMode, controls};
use lightcraft_geom::{Orientation, Rect};
use lightcraft_pipeline::{Quality, RenderRequest, SourceInfo, StageCache, auto, profiles};
use lightcraft_raster::Rgb32f;
use wasm_bindgen::prelude::*;

/// Lado mayor maximo que se acepta revelar (proteccion de memoria: 65 Mpx).
const MAX_SIDE: u32 = 12_000;

fn err(msg: impl std::fmt::Display) -> JsValue {
    JsValue::from_str(&msg.to_string())
}

/// Escala relativa de temperatura para fotos ya reveladas (JPEG): -100..100
/// alrededor de 6500 K, en mireds, igual que la interfaz de LightCraft.
fn rel_to_k(r: f64) -> f64 {
    1e6 / (1e6 / 6500.0 - r.clamp(-100.0, 100.0) * 0.8)
}
fn k_to_rel(k: f64) -> f64 {
    ((1e6 / 6500.0 - 1e6 / k.max(1.0)) / 0.8).clamp(-100.0, 100.0)
}

/// Tabla de controles de LightCraft (id, etiqueta, seccion, rango, defecto,
/// paso, decimales, tipo de pista) en JSON.
#[wasm_bindgen]
pub fn controls_json() -> String {
    serde_json::to_string(controls::CONTROLS).unwrap_or_else(|_| "[]".into())
}

/// Perfiles creativos disponibles (ademas de `lc.color`, el neutro).
#[wasm_bindgen]
pub fn profiles_json() -> String {
    serde_json::to_string(profiles::LOOK_IDS).unwrap_or_else(|_| "[]".into())
}

/// Una foto abierta en el editor.
#[wasm_bindgen]
pub struct Editor {
    src: Arc<Rgb32f>,
    info: SourceInfo,
    settings: DevelopSettings,
    cache: StageCache,
    out_w: usize,
    out_h: usize,
    histogram: Vec<u32>,
}

#[wasm_bindgen]
impl Editor {
    /// Decodifica `bytes`. `orientation`: valor EXIF 1..8 a aplicar, o 0 para
    /// usar el del propio archivo. `max_side`: lado mayor de trabajo (0 =
    /// resolucion completa; la vista previa trabaja con ~2000 px).
    #[wasm_bindgen(constructor)]
    pub fn new(bytes: &[u8], orientation: u16, max_side: u32) -> Result<Editor, JsValue> {
        let side = if max_side == 0 { MAX_SIDE } else { max_side.min(MAX_SIDE) };
        let opts = DecodeOptions::fit(side, side);
        let d = lightcraft_codecs::decode(bytes, opts).map_err(err)?;
        let o = if orientation == 0 { d.orientation } else { orientation };
        let src = d.to_working().into_oriented(Orientation::from_exif(o));
        Ok(Editor {
            src: Arc::new(src),
            info: SourceInfo::default(),
            settings: DevelopSettings::default(),
            cache: StageCache::default(),
            out_w: 0,
            out_h: 0,
            histogram: Vec::new(),
        })
    }

    /// Tamano de la foto de trabajo (ya orientada segun EXIF).
    pub fn width(&self) -> usize {
        self.src.width
    }
    pub fn height(&self) -> usize {
        self.src.height
    }

    /// Lee un control por id. Ademas de los de LightCraft: `wb.tempRel` y
    /// `wb.tintRel` (balance de blancos relativo, el de las fotos JPEG).
    pub fn get(&self, id: &str) -> f64 {
        let s = &self.settings;
        let as_shot = s.wb.mode == WbMode::AsShot;
        match id {
            "wb.tempRel" => if as_shot { 0.0 } else { k_to_rel(s.wb.temp) },
            "wb.tintRel" => if as_shot { 0.0 } else { s.wb.tint.clamp(-100.0, 100.0) },
            _ => controls::get(s, id).unwrap_or(0.0),
        }
    }

    /// Cambia un control por id (se recorta a su rango). Devuelve false si el
    /// id no existe.
    pub fn set(&mut self, id: &str, v: f64) -> bool {
        match id {
            "wb.tempRel" | "wb.tintRel" => {
                let (mut t, mut n) = (self.get("wb.tempRel"), self.get("wb.tintRel"));
                if id == "wb.tempRel" { t = v.clamp(-100.0, 100.0) } else { n = v.clamp(-100.0, 100.0) }
                let wb = &mut self.settings.wb;
                if t == 0.0 && n == 0.0 {
                    wb.mode = WbMode::AsShot;
                    wb.temp = 6500.0;
                    wb.tint = 0.0;
                } else {
                    wb.mode = WbMode::Custom;
                    wb.temp = rel_to_k(t);
                    wb.tint = n;
                }
                true
            }
            _ => controls::set(&mut self.settings, id, v),
        }
    }

    /// Blanco y negro (tratamiento de LightCraft).
    pub fn set_bw(&mut self, on: bool) {
        self.settings.treatment = if on { Treatment::Bw } else { Treatment::Color };
    }
    pub fn bw(&self) -> bool {
        self.settings.treatment == Treatment::Bw
    }

    /// Perfil creativo (`lc.color` = ninguno) y su intensidad 0..200.
    pub fn set_profile(&mut self, id: &str, amount: f64) {
        self.settings.profile.id = if id.is_empty() { "lc.color".into() } else { id.into() };
        self.settings.profile.amount = amount.clamp(0.0, 200.0);
    }
    pub fn profile(&self) -> String {
        self.settings.profile.id.clone()
    }

    /// Giro/volteo del usuario, como valor EXIF 1..8 sobre la foto ya orientada.
    pub fn set_orientation(&mut self, exif: u16) {
        self.settings.orientation = Orientation::from_exif(exif);
    }

    /// Recorte normalizado (0..1) en el marco girado `angle` grados (horario).
    pub fn set_crop(&mut self, x0: f64, y0: f64, x1: f64, y1: f64, angle: f64) {
        let g = &mut self.settings.crop.geometry;
        g.rect = Rect { x0: x0.clamp(0.0, 1.0), y0: y0.clamp(0.0, 1.0), x1: x1.clamp(0.0, 1.0), y1: y1.clamp(0.0, 1.0) };
        g.angle = if angle.is_finite() { angle.clamp(-45.0, 45.0) } else { 0.0 };
    }

    /// El mayor recorte que cabe con ese angulo (y proporcion ancho/alto, 0 =
    /// la de la foto), en un marco de `w × h`. Devuelve [x0, y0, x1, y1].
    pub fn fit_crop(w: f64, h: f64, angle: f64, aspect: f64) -> Vec<f64> {
        let a = if aspect > 0.0 { Some(aspect) } else { None };
        let c = lightcraft_geom::crop_fit_angle(w.max(1.0), h.max(1.0), angle, a);
        vec![c.rect.x0, c.rect.y0, c.rect.x1, c.rect.y1]
    }

    /// Auto (tono y viveza como el Auto de Lightroom). Devuelve los valores en JSON.
    pub fn auto_tone(&mut self) -> String {
        let a = auto::auto_tone(&self.src, &self.info, &self.settings);
        let l = &mut self.settings.light;
        l.exposure = a.exposure;
        l.contrast = a.contrast;
        l.highlights = a.highlights;
        l.shadows = a.shadows;
        l.whites = a.whites;
        l.blacks = a.blacks;
        self.settings.color.vibrance = a.vibrance;
        self.settings.color.saturation = a.saturation;
        format!(
            "{{\"light.exposure\":{},\"light.contrast\":{},\"light.highlights\":{},\"light.shadows\":{},\"light.whites\":{},\"light.blacks\":{},\"color.vibrance\":{},\"color.saturation\":{}}}",
            a.exposure, a.contrast, a.highlights, a.shadows, a.whites, a.blacks, a.vibrance, a.saturation
        )
    }

    /// Balance de blancos automatico (escala relativa). Devuelve [temp, tint].
    pub fn auto_wb(&mut self) -> Vec<f64> {
        let (t, n) = auto::auto_wb(&self.src, &self.info);
        let wb = &mut self.settings.wb;
        wb.mode = WbMode::Custom;
        wb.temp = t;
        wb.tint = n;
        vec![k_to_rel(t), n.clamp(-100.0, 100.0)]
    }

    /// Todos los ajustes en JSON (para deshacer y para pasar la edicion al
    /// revelado a resolucion completa).
    pub fn settings_json(&self) -> String {
        serde_json::to_string(&self.settings).unwrap_or_default()
    }
    pub fn load_settings_json(&mut self, json: &str) -> Result<(), JsValue> {
        self.settings = serde_json::from_str(json).map_err(err)?;
        Ok(())
    }
    pub fn reset(&mut self) {
        self.settings = DevelopSettings::default();
    }
    /// Hay algo que guardar (distinto de los ajustes por defecto).
    pub fn is_default(&self) -> bool {
        self.settings == DevelopSettings::default()
    }

    /// Revela dentro de `max_w × max_h` y devuelve RGBA de 8 bits. `draft`:
    /// calidad de arrastre (mas rapida). `apply_crop`: false enseña la foto
    /// entera (herramienta de recorte). `before`: el "antes" (sin ajustes,
    /// con el mismo encuadre).
    pub fn render(&mut self, max_w: usize, max_h: usize, draft: bool, apply_crop: bool, before: bool) -> Vec<u8> {
        // El pipeline amplia si se le pide mas que la foto: nunca por encima
        // de su tamano real (tras recorte y giro)
        let (nw, nh) = if apply_crop {
            lightcraft_pipeline::native_output_size(self.src.width, self.src.height, &self.settings)
        } else {
            let mut whole = self.settings.clone();
            whole.crop.geometry = lightcraft_geom::CropGeometry::default();
            lightcraft_pipeline::native_output_size(self.src.width, self.src.height, &whole)
        };
        let scale = (nw / max_w.max(1) as f64).max(nh / max_h.max(1) as f64).max(1.0);
        let fw = ((nw / scale).round() as usize).clamp(1, max_w.max(1));
        let fh = ((nh / scale).round() as usize).clamp(1, max_h.max(1));
        let mut req = RenderRequest::fit(fw, fh);
        req.quality = if draft { Quality::Draft } else { Quality::Full };
        req.apply_crop = apply_crop;
        let r = if before {
            let b = lightcraft_pipeline::before_settings(&self.settings);
            lightcraft_pipeline::render(&self.src, &self.info, &b, &req)
        } else {
            lightcraft_pipeline::render_cached(&self.src, &self.info, &self.settings, &req, &self.cache)
        };
        self.out_w = r.image.width;
        self.out_h = r.image.height;
        let h = &r.histogram;
        self.histogram = h.r.iter().chain(&h.g).chain(&h.b).chain(&h.luma).copied().collect();
        r.image.as_bytes()
    }

    /// Tamano del ultimo revelado.
    pub fn out_width(&self) -> usize {
        self.out_w
    }
    pub fn out_height(&self) -> usize {
        self.out_h
    }
    /// Histograma del ultimo revelado: 256 valores de R, G, B y luminancia.
    pub fn histogram(&self) -> Vec<u32> {
        self.histogram.clone()
    }

    /// Revela a resolucion completa y lo codifica (JPEG con `quality` 1..100,
    /// 4:4:4; o PNG sin perdida si `png`). Sin metadatos: el servidor copia
    /// los del original.
    pub fn export(&mut self, png: bool, quality: u8) -> Result<Vec<u8>, JsValue> {
        let (w, h) = lightcraft_pipeline::native_output_size(self.src.width, self.src.height, &self.settings);
        let req = RenderRequest::fit(w.round().max(1.0) as usize, h.round().max(1.0) as usize);
        let r = lightcraft_pipeline::render(&self.src, &self.info, &self.settings, &req);
        self.cache.clear();
        let img = EncodeImage::rgba8(&r.image);
        let meta = EncodeMeta::default();
        let out = if png {
            lightcraft_codecs::encode_png(&img, &meta)
        } else {
            lightcraft_codecs::encode_jpeg(&img, quality.clamp(1, 100), ChromaSubsampling::S444, &meta)
        };
        out.map_err(err)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn jpeg(w: usize, h: usize) -> Vec<u8> {
        let mut img = lightcraft_raster::Rgba8::new(w, h);
        for (i, p) in img.data.iter_mut().enumerate() {
            let (x, y) = (i % w, i / w);
            *p = [(x * 255 / w) as u8, (y * 255 / h) as u8, 120, 255];
        }
        lightcraft_codecs::encode_jpeg(&EncodeImage::rgba8(&img), 95, ChromaSubsampling::S444, &EncodeMeta::default()).unwrap()
    }

    #[test]
    fn neutral_render_keeps_the_photo() {
        let mut e = Editor::new(&jpeg(320, 200), 0, 0).unwrap();
        assert_eq!((e.width(), e.height()), (320, 200));
        let px = e.render(160, 160, false, true, false);
        assert_eq!((e.out_width(), e.out_height()), (160, 100));
        assert_eq!(px.len(), 160 * 100 * 4);
        assert_eq!(e.histogram().len(), 1024);
        assert!(e.is_default());
    }

    #[test]
    fn controls_change_the_render() {
        let mut e = Editor::new(&jpeg(200, 200), 0, 0).unwrap();
        let a = e.render(100, 100, false, true, false);
        assert!(e.set("light.exposure", 1.0));
        assert!(!e.set("no.existe", 1.0));
        let b = e.render(100, 100, false, true, false);
        let mean = |v: &[u8]| v.iter().map(|&x| x as u64).sum::<u64>() / v.len() as u64;
        assert!(mean(&b) > mean(&a));
        assert!(!e.is_default());
    }

    #[test]
    fn relative_white_balance_round_trips() {
        let mut e = Editor::new(&jpeg(64, 64), 0, 0).unwrap();
        e.set("wb.tempRel", 30.0);
        assert!((e.get("wb.tempRel") - 30.0).abs() < 1e-6);
        e.set("wb.tempRel", 0.0);
        e.set("wb.tintRel", 0.0);
        assert_eq!(e.settings.wb.mode, WbMode::AsShot);
    }

    #[test]
    fn crop_rotation_and_export() {
        let mut e = Editor::new(&jpeg(300, 200), 0, 0).unwrap();
        e.set_orientation(6);
        e.render(1000, 1000, false, true, false);
        assert_eq!((e.out_width(), e.out_height()), (200, 300));
        e.set_orientation(1);
        e.set_crop(0.0, 0.0, 0.5, 0.5, 0.0);
        let jpg = e.export(false, 90).unwrap();
        let back = lightcraft_codecs::decode(&jpg, DecodeOptions::default()).unwrap();
        assert_eq!((back.width, back.height), (150, 100));
        let r = Editor::fit_crop(300.0, 200.0, 10.0, 0.0);
        assert!(r[0] > 0.0 && r[2] < 1.0);
    }

    #[test]
    fn settings_round_trip_and_auto() {
        let mut e = Editor::new(&jpeg(128, 96), 0, 0).unwrap();
        e.set("effects.clarity", 40.0);
        e.set_bw(true);
        e.set_profile("lc.vivid", 80.0);
        let js = e.settings_json();
        let mut f = Editor::new(&jpeg(128, 96), 0, 0).unwrap();
        f.load_settings_json(&js).unwrap();
        assert_eq!(f.get("effects.clarity"), 40.0);
        assert!(f.bw());
        assert_eq!(f.profile(), "lc.vivid");
        assert!(f.auto_tone().contains("light.exposure"));
        assert!(controls_json().contains("light.exposure"));
        assert!(profiles_json().contains("lc.vivid"));
    }
}
