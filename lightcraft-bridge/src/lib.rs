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
use lightcraft_geom::{Orientation, Point, Rect};
use lightcraft_pipeline::{MaskView, Overlay, Quality, RenderRequest, SourceInfo, StageCache, auto, profiles};
use lightcraft_raster::Rgb32f;
use wasm_bindgen::prelude::*;

// Objetivos del Auto (luminancia de salida, 0..255)
const KEY_TARGET: f64 = 118.0;     // tono medio
const WHITE_TARGET: f64 = 246.0;   // percentil 99,5
const BLACK_TARGET: f64 = 5.0;     // percentil 0,5
const SHADOW_P10: f64 = 26.0;      // percentil 10: sombras grandes por debajo = foto empastada
const CLIP_HI_MAX: f64 = 0.004;    // fraccion maxima de pixeles quemados
const AUTO_SIDE: usize = 320;      // tamano al que se mide
const AUTO_IDS: [&str; 8] = ["light.exposure", "light.contrast", "light.highlights", "light.shadows",
                             "light.whites", "light.blacks", "color.vibrance", "color.saturation"];

/// Los ajustes que de verdad se revelan.
///
/// Blancos y Negros del motor estan calibrados para RAW (luz lineal de
/// escena): con un JPEG, que ya viene revelado, Negros -100..+100 movia el
/// punto negro dos niveles y Blancos unos quince; parecian no hacer nada.
/// Aqui actuan como en Lightroom con JPEG: sobre la imagen ya codificada, en
/// el extremo que les toca (Blancos sube o baja las luces altas hasta
/// quemar o apagar el blanco; Negros hunde o levanta las sombras profundas),
/// y se componen ANTES de la curva maestra del usuario.
fn effective(s: &DevelopSettings) -> DevelopSettings {
    let mut e = s.clone();
    let (w, b) = (s.light.whites / 100.0, s.light.blacks / 100.0);
    if w == 0.0 && b == 0.0 {
        return e;
    }
    e.light.whites = 0.0;
    e.light.blacks = 0.0;
    let smooth = |e0: f64, e1: f64, x: f64| {
        let t = ((x - e0) / (e1 - e0)).clamp(0.0, 1.0);
        t * t * (3.0 - 2.0 * t)
    };
    let levels = |x: f64| (x + 0.25 * w * smooth(0.35, 1.0, x) + 0.2 * b * (1.0 - smooth(0.0, 0.6, x))).clamp(0.0, 1.0);
    let user = &s.curve.master;
    let user_curve = if lightcraft_develop::ToneCurve::point_curve_is_identity(user) {
        None
    } else {
        let pts: Vec<(f64, f64)> = user.iter().map(|p| (p.x, p.y)).collect();
        Some(lightcraft_color::spline::MonotoneCurve::new(&pts))
    };
    const N: usize = 48;
    e.curve.master = (0..=N)
        .map(|i| {
            let x = i as f64 / N as f64;
            let y = levels(x);
            Point::new(x, user_curve.as_ref().map_or(y, |c| c.eval(y)))
        })
        .collect();
    e
}

/// Lo que el Auto mira del histograma de luminancia del resultado.
struct Stats {
    key: f64,
    p005: f64,
    p10: f64,
    p995: f64,
    clip_hi: f64,
    clip_lo: f64,
}

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
    /// Mascara cuya superposicion se pinta (-1 = ninguna)
    overlay: i32,
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
            overlay: -1,
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

    /// Auto de Geotagger: ajusta Exposicion, Iluminaciones, Sombras, Blancos,
    /// Negros, Contraste e Intensidad MIDIENDO el resultado.
    ///
    /// El Auto de LightCraft aplica una receta fija (Contraste +68,
    /// Iluminaciones -66, Sombras +52) ajustada para RAW; con JPEG, que ya
    /// vienen revelados, comprimia el rango (blancos a ~200, negros
    /// levantados) y oscurecia: fotos grises, sin visibilidad. Este revela
    /// una version pequena, mira su histograma y corrige cada control hasta
    /// llegar a unos objetivos (lazo cerrado), asi que el resultado es el
    /// que se busca sea cual sea la foto. Devuelve los valores en JSON.
    pub fn auto_tone(&mut self) -> String {
        let mut s = self.settings.clone();
        for id in AUTO_IDS {
            if let Some(sp) = controls::find(id) {
                controls::set(&mut s, id, sp.default);
            }
        }
        s.curve.master = Vec::new();
        // Objetivo de tono medio: se acerca a KEY_TARGET pero conserva un 30 %
        // del caracter de la foto (una noche sigue siendo oscura, una nevada clara)
        let key0 = self.measure(&s).key;
        let target = KEY_TARGET - 0.3 * (KEY_TARGET - key0);

        // 1. Exposicion: el tono medio (media recortada) a un gris medio
        //    luminoso, sin quemar mas de un 1,5 % de la foto al subirla
        let ev_key = self.solve(&mut s, "light.exposure", -3.0, 3.5, |m| m.key - target);
        let ev = if ev_key > 0.0 {
            let ev_clip = self.solve(&mut s, "light.exposure", 0.0, 3.5, |m| m.clip_hi - 0.015);
            ev_key.min(ev_clip.max(0.0))
        } else {
            ev_key
        };
        controls::set(&mut s, "light.exposure", ev);

        // 2. Iluminaciones: rescatar lo quemado, con mesura (a tope aplana el cielo)
        let m = self.measure(&s);
        if m.clip_hi > CLIP_HI_MAX {
            let h = self.solve(&mut s, "light.highlights", -60.0, 0.0, |m| CLIP_HI_MAX - m.clip_hi);
            controls::set(&mut s, "light.highlights", h);
        }
        // 3. Sombras: abrir zonas oscuras grandes, sin tocar un fondo negro puro
        let m = self.measure(&s);
        if m.p10 < SHADOW_P10 && m.clip_lo < 0.08 {
            let sh = self.solve(&mut s, "light.shadows", 0.0, 50.0, |m| m.p10 - SHADOW_P10);
            controls::set(&mut s, "light.shadows", sh);
        }
        // 4. Niveles en la curva maestra: el 0,5 % mas oscuro a negro y el
        //    0,5 % mas claro a blanco. Es lo que de verdad devuelve el
        //    contraste a una foto desvaida (Blancos/Negros del motor se
        //    quedan cortos con JPEG) y queda a la vista en la curva.
        let m = self.measure(&s);
        let bx = if m.p005 > BLACK_TARGET + 2.0 { ((m.p005 - BLACK_TARGET) / 255.0).min(0.30) } else { 0.0 };
        let wx = if m.p995 < WHITE_TARGET - 2.0 { ((m.p995 + 255.0 - WHITE_TARGET) / 255.0).max(0.55) } else { 1.0 };
        if bx > 0.0 || wx < 1.0 || (m.key - target).abs() > 6.0 {
            // 5. Tono medio con un punto central (el "gamma" de los niveles):
            //    sin volver a tocar la exposicion, que bajaria otra vez los blancos
            let mid_x = bx + (wx - bx) * 0.5;
            let curve = |my: f64| vec![Point::new(bx, 0.0), Point::new(mid_x, my), Point::new(wx, 1.0)];
            let (mut lo, mut hi) = (0.30, 0.70);
            for _ in 0..9 {
                let my = (lo + hi) / 2.0;
                s.curve.master = curve(my);
                if self.measure(&s).key < target { lo = my } else { hi = my }
            }
            let my = ((lo + hi) / 2.0 * 1000.0).round() / 1000.0;
            s.curve.master = if bx == 0.0 && wx == 1.0 && (my - 0.5).abs() < 0.01 { Vec::new() } else { curve(my) };
        }
        // (Sin Intensidad ni Saturacion: estirar el histograma ya aviva el color)

        let mut out = Vec::new();
        for id in AUTO_IDS {
            let v = controls::get(&s, id).unwrap_or(0.0);
            let v = if id == "light.exposure" { (v * 100.0).round() / 100.0 } else { v.round() };
            controls::set(&mut s, id, v);
            out.push(format!("\"{id}\":{v}"));
        }
        let curve: Vec<String> = s.curve.master.iter().flat_map(|p| [format!("{:.4}", p.x), format!("{:.4}", p.y)]).collect();
        out.push(format!("\"curve.master\":[{}]", curve.join(",")));
        self.settings = s;
        format!("{{{}}}", out.join(","))
    }

    /// El Auto anterior (el de LightCraft), por si se quiere comparar.
    pub fn auto_tone_lightcraft(&mut self) -> String {
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

    /// Curva por puntos de un canal (`master`, `red`, `green`, `blue`):
    /// pares x,y en 0..1 sobre la imagen ya codificada, como en Lightroom.
    /// Menos de dos puntos = curva neutra.
    pub fn set_curve(&mut self, channel: &str, xy: &[f64]) {
        let pts: Vec<Point> = xy
            .chunks_exact(2)
            .filter(|p| p[0].is_finite() && p[1].is_finite())
            .map(|p| Point::new(p[0].clamp(0.0, 1.0), p[1].clamp(0.0, 1.0)))
            .collect();
        let pts = if pts.len() < 2 { Vec::new() } else { pts };
        let c = &mut self.settings.curve;
        match channel {
            "master" => c.master = pts,
            "red" => c.red = pts,
            "green" => c.green = pts,
            "blue" => c.blue = pts,
            _ => {}
        }
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
        if self.overlay >= 0 && !before {
            req.overlay = Overlay::Mask { id: self.overlay as u16, view: MaskView::Color, color: [255, 48, 64], opacity: 55 };
        }
        let r = if before {
            let b = lightcraft_pipeline::before_settings(&self.settings);
            lightcraft_pipeline::render(&self.src, &self.info, &b, &req)
        } else {
            let eff = effective(&self.settings);
            lightcraft_pipeline::render_cached(&self.src, &self.info, &eff, &req, &self.cache)
        };
        self.out_w = r.image.width;
        self.out_h = r.image.height;
        let h = &r.histogram;
        self.histogram = h.r.iter().chain(&h.g).chain(&h.b).chain(&h.luma).copied().collect();
        r.image.as_bytes()
    }

    /// Capas de mascara (formato `Mask` de LightCraft, en JSON): cada una con
    /// sus formas (pincel, degradado lineal o radial, rango de luminancia o de
    /// color, cielo/sujeto/fondo automaticos) y sus ajustes locales.
    pub fn set_masks_json(&mut self, json: &str) -> Result<(), JsValue> {
        self.settings.masks = serde_json::from_str(json).map_err(err)?;
        Ok(())
    }
    /// Mascara de segmentacion calculada fuera (IA en el navegador): `kind`
    /// "person" (la usan Sujeto y Fondo) o "sky". `data`: alfa 0..255 de
    /// `w × h` que cubre la foto entera ya orientada (a cualquier resolucion).
    pub fn set_matte(&mut self, kind: &str, w: usize, h: usize, data: &[u8]) -> bool {
        use lightcraft_pipeline::masks::{MatteKind, Mattes};
        let k = match kind {
            "person" | "subject" => MatteKind::Person,
            "sky" => MatteKind::Sky,
            _ => return false,
        };
        if w == 0 || h == 0 || data.len() != w * h {
            return false;
        }
        let mut m: Mattes = self.info.mattes.as_deref().cloned().unwrap_or_default();
        m.push(k, lightcraft_raster::Image { width: w, height: h, data: data.to_vec() });
        self.info.mattes = Some(Arc::new(m));
        self.cache.clear();
        true
    }
    pub fn clear_mattes(&mut self) {
        self.info.mattes = None;
        self.cache.clear();
    }
    /// La foto tal cual (sin ajustes, giro ni recorte) dentro de `max_w × max_h`:
    /// la entrada de los modelos de segmentacion.
    pub fn neutral_rgba(&mut self, max_w: usize, max_h: usize) -> Vec<u8> {
        let req = RenderRequest::fit(max_w.max(1), max_h.max(1));
        let r = lightcraft_pipeline::render(&self.src, &self.info, &DevelopSettings::default(), &req);
        self.out_w = r.image.width;
        self.out_h = r.image.height;
        r.image.as_bytes()
    }

    /// Correcciones de manchas (formato `Spot` de LightCraft, en JSON).
    pub fn set_spots_json(&mut self, json: &str) -> Result<(), JsValue> {
        self.settings.spots = serde_json::from_str(json).map_err(err)?;
        Ok(())
    }
    /// Origen para la correccion `spot_json` (desplazamiento normalizado
    /// [dx, dy]): el que mejor encaja con el entorno de la mancha, o con
    /// `avoid` el mejor que este lejos del actual ("buscar otro origen").
    /// Vacio si no cabe ninguno. Se fija al crear la correccion para que la
    /// vista previa y la foto guardada usen el mismo.
    pub fn pick_spot_source(&self, spot_json: &str, avoid_x: f64, avoid_y: f64, use_avoid: bool) -> Vec<f64> {
        let Ok(spot) = serde_json::from_str::<lightcraft_develop::Spot>(spot_json) else { return Vec::new() };
        let avoid = use_avoid.then(|| Point::new(avoid_x, avoid_y));
        let eff = effective(&self.settings);
        lightcraft_pipeline::spots::pick_source(&self.src, &self.info, &eff, &spot, avoid)
            .map(|p| vec![p.x, p.y])
            .unwrap_or_default()
    }

    /// Pintar la superposicion roja de la mascara `id` (-1: ninguna).
    pub fn set_overlay(&mut self, id: i32) {
        self.overlay = id;
    }
    /// Color (OkLab) que tomaria una mascara de rango de color en el punto
    /// normalizado (x, y) de la foto. Vacio si cae fuera.
    pub fn color_sample(&self, x: f64, y: f64) -> Vec<f64> {
        let req = RenderRequest::fit(512, 512);
        lightcraft_pipeline::color_range_sample(&self.src, &self.info, &effective(&self.settings), &req, Point::new(x, y))
            .map(|v| v.to_vec())
            .unwrap_or_default()
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
        let eff = effective(&self.settings);
        let (w, h) = lightcraft_pipeline::native_output_size(self.src.width, self.src.height, &eff);
        let req = RenderRequest::fit(w.round().max(1.0) as usize, h.round().max(1.0) as usize);
        let r = lightcraft_pipeline::render(&self.src, &self.info, &eff, &req);
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

impl Editor {
    /// Revela pequeno con los ajustes `s` y mide el histograma de luminancia.
    fn measure(&self, s: &DevelopSettings) -> Stats {
        let mut req = RenderRequest::fit(AUTO_SIDE, AUTO_SIDE);
        req.quality = Quality::Draft;
        let img = lightcraft_pipeline::render(&self.src, &self.info, &effective(s), &req).image;
        let mut l: Vec<f64> = img
            .data
            .iter()
            .map(|p| 0.2126 * p[0] as f64 + 0.7152 * p[1] as f64 + 0.0722 * p[2] as f64)
            .collect();
        if l.is_empty() {
            l.push(118.0);
        }
        l.sort_by(|a, b| a.total_cmp(b));
        let n = l.len();
        let q = |p: f64| l.get(((n - 1) as f64 * p).round() as usize).copied().unwrap_or(0.0);
        // Tono medio sin los negros ni los blancos puros: un fondo negro o un
        // cielo quemado no deben decidir la exposicion del sujeto
        // (si lo quemado es mucho, la foto esta sobreexpuesta y si cuenta)
        let hi_cut = if l.iter().filter(|v| **v >= 251.0).count() * 20 > n { 256.0 } else { 251.0 };
        let mid: Vec<f64> = l.iter().copied().filter(|v| *v > 4.0 && *v < hi_cut).collect();
        let key = if mid.len() * 10 >= n {
            mid.iter().sum::<f64>() / mid.len() as f64
        } else {
            l.iter().sum::<f64>() / n as f64
        };
        Stats {
            key,
            p005: q(0.005),
            p10: q(0.10),
            p995: q(0.995),
            clip_hi: l.iter().filter(|v| **v >= 252.0).count() as f64 / n as f64,
            clip_lo: l.iter().filter(|v| **v <= 2.0).count() as f64 / n as f64,
        }
    }

    /// Busca (biseccion) el valor de `id` en [lo, hi] que lleva `f` a cero;
    /// `f` crece con el control. Si no hay cero en el rango, el extremo mas cercano.
    fn solve(&self, s: &mut DevelopSettings, id: &str, lo: f64, hi: f64, f: impl Fn(&Stats) -> f64) -> f64 {
        let at = |s: &mut DevelopSettings, v: f64| {
            controls::set(s, id, v);
            f(&self.measure(s))
        };
        let (mut lo, mut hi) = (lo, hi);
        if at(s, lo) >= 0.0 {
            return lo;
        }
        if at(s, hi) <= 0.0 {
            return hi;
        }
        for _ in 0..9 {
            let mid = (lo + hi) / 2.0;
            if at(s, mid) < 0.0 { lo = mid } else { hi = mid }
        }
        (lo + hi) / 2.0
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
    fn curves_and_auto_improve_a_dark_photo() {
        let mut img = lightcraft_raster::Rgba8::new(200, 150);
        for (i, p) in img.data.iter_mut().enumerate() {
            let v = ((i % 200) * 60 / 200) as u8 + 5; // foto oscura y plana: 5..65
            *p = [v, v, v.saturating_add(4), 255];
        }
        let jpg = lightcraft_codecs::encode_jpeg(&EncodeImage::rgba8(&img), 95, ChromaSubsampling::S444, &EncodeMeta::default()).unwrap();
        let mut e = Editor::new(&jpg, 0, 0).unwrap();
        let before = e.measure(&e.settings.clone());
        e.auto_tone();
        let after = e.measure(&e.settings.clone());
        assert!(after.key > before.key + 40.0, "{} -> {}", before.key, after.key);
        assert!(after.p995 - after.p005 > before.p995 - before.p005 + 80.0);
        // curva maestra que aclara los medios
        let mut f = Editor::new(&jpg, 0, 0).unwrap();
        let m0 = f.measure(&f.settings.clone()).key;
        f.set_curve("master", &[0.0, 0.0, 0.25, 0.5, 1.0, 1.0]);
        assert!(f.measure(&f.settings.clone()).key > m0 + 20.0);
        f.set_curve("red", &[0.0, 0.0, 1.0, 0.5]);
        assert!(!f.is_default());
        f.set_curve("master", &[]);
        f.set_curve("red", &[]);
        assert!(f.is_default());
    }

    #[test]
    fn whites_blacks_and_masks_act_on_jpeg() {
        let mut img = lightcraft_raster::Rgba8::new(120, 80);
        for (i, p) in img.data.iter_mut().enumerate() {
            let v = (20 + (i % 120) * 200 / 120) as u8; // 20..220
            *p = [v, v, v, 255];
        }
        let jpg = lightcraft_codecs::encode_jpeg(&EncodeImage::rgba8(&img), 95, ChromaSubsampling::S444, &EncodeMeta::default()).unwrap();
        let mut e = Editor::new(&jpg, 0, 0).unwrap();
        let base = e.measure(&e.settings.clone());
        e.set("light.blacks", -100.0);
        let crushed = e.measure(&e.settings.clone());
        assert!(crushed.p005 + 15.0 < base.p005, "{} -> {}", base.p005, crushed.p005);
        e.set("light.blacks", 0.0);
        e.set("light.whites", 100.0);
        let bright = e.measure(&e.settings.clone());
        assert!(bright.p995 > base.p995 + 15.0, "{} -> {}", base.p995, bright.p995);
        // una mascara radial con +2 EV aclara el centro y no las esquinas
        let mut f = Editor::new(&jpg, 0, 0).unwrap();
        f.set_masks_json(r#"[{"id":1,"name":"m","visible":true,"invert":false,"components":[{"op":"add","invert":false,
            "shape":{"kind":"radial","center":{"x":0.5,"y":0.5},"rx":0.15,"ry":0.15,"angle":0,"feather":0,"invert":false}}],
            "adjust":{"exposure":2}}]"#).unwrap();
        let px = f.render(120, 80, false, true, false);
        let at = |x: usize, y: usize| px[(y * 120 + x) * 4] as i32;
        let mut g = Editor::new(&jpg, 0, 0).unwrap();
        let px0 = g.render(120, 80, false, true, false);
        let at0 = |x: usize, y: usize| px0[(y * 120 + x) * 4] as i32;
        assert!(at(60, 40) > at0(60, 40) + 30);
        assert!((at(3, 3) - at0(3, 3)).abs() < 4);
        assert_eq!(f.color_sample(0.5, 0.5).len(), 3);
    }

    #[test]
    fn mattes_and_spots() {
        // mitad izquierda oscura, derecha clara; un punto negro en la derecha
        let mut img = lightcraft_raster::Rgba8::new(160, 100);
        for (i, p) in img.data.iter_mut().enumerate() {
            let (x, y) = (i % 160, i / 160);
            let v = if x < 80 { 40 } else { 200 };
            let spot = (x as i32 - 120).pow(2) + (y as i32 - 50).pow(2) < 16;
            *p = if spot { [0, 0, 0, 255] } else { [v, v, v, 255] };
        }
        let jpg = lightcraft_codecs::encode_jpeg(&EncodeImage::rgba8(&img), 98, ChromaSubsampling::S444, &EncodeMeta::default()).unwrap();
        let mut e = Editor::new(&jpg, 0, 0).unwrap();
        assert_eq!(e.neutral_rgba(160, 160).len(), 160 * 100 * 4);
        // matte de "persona" = mitad izquierda; Sujeto +2 EV aclara solo esa mitad
        let matte: Vec<u8> = (0..40 * 25).map(|i| if i % 40 < 20 { 255 } else { 0 }).collect();
        assert!(e.set_matte("person", 40, 25, &matte));
        e.set_masks_json(r#"[{"id":1,"name":"s","visible":true,"invert":false,"components":[{"op":"add","invert":false,"shape":{"kind":"subject"}}],"adjust":{"exposure":2}}]"#).unwrap();
        let px = e.render(160, 100, false, true, false);
        assert!(px[(50 * 160 + 20) * 4] > 70, "izquierda aclarada: {}", px[(50 * 160 + 20) * 4]);
        assert!(px[(50 * 160 + 140) * 4] < 215);
        // corrector: el punto negro desaparece con origen elegido automaticamente
        let mut f = Editor::new(&jpg, 0, 0).unwrap();
        let spot = r#"{"mode":"remove","points":[{"x":0.75,"y":0.5}],"size":0.05,"feather":50,"opacity":100,"source_offset":null}"#;
        let src = f.pick_spot_source(spot, 0.0, 0.0, false);
        assert_eq!(src.len(), 2);
        f.set_spots_json(&format!(r#"[{{"mode":"remove","points":[{{"x":0.75,"y":0.5}}],"size":0.05,"feather":50,"opacity":100,"source_offset":{{"x":{},"y":{}}}}}]"#, src[0], src[1])).unwrap();
        let px = f.render(160, 100, false, true, false);
        assert!(px[(50 * 160 + 120) * 4] > 150, "mancha corregida: {}", px[(50 * 160 + 120) * 4]);
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
