// Hilo de revelado del editor: aqui vive el motor de LightCraft (WebAssembly).
// Corre aparte para que mover un deslizador nunca congele la interfaz.
//
// Mensajes (todos llevan `seq`, que vuelve en la respuesta):
//   open    {url}                         -> opened {w, h, controls, profiles}
//   render  {st, maxW, maxH, draft, applyCrop, before}
//                                         -> rendered {w, h, px, hist}
//   auto    {st}                          -> auto {values}
//   autoWb  {st}                          -> autoWb {temp, tint}
//   export  {url, st, png, quality}       -> exported {bytes}
//   cualquier fallo                       -> error {msg}
import init, { Editor, controls_json, profiles_json } from "./geotagger_lightcraft.js";

const ready = init();
let ed = null;           // la foto de trabajo (~2048 px)
let srcBytes = null, srcOrient = 1, srcUrl = null;   // el original, para guardar sin volver a bajarlo

// ── IA: sujeto y cielo ───────────────────────────────────────────────────────
// Dos modelos U^2-Net pequenos (ai/NOTICE.md) con ONNX Runtime Web, cargados
// solo cuando se pide Sujeto, Fondo o Cielo. Se calculan una vez por foto
// sobre la vista previa y se le dan al motor como mascaras de segmentacion
// ("mattes"): LightCraft las usa en vez de su heuristica, y valen a cualquier
// resolucion (tambien al guardar la foto entera).
const AI = {
  person: { model: "u2netp", size: 320, minmax: true },
  sky: { model: "skyseg", size: 384, minmax: false },
};
let ort = null;
const sessions = {};
let mattes = {};         // kind -> { w, h, data } de la foto abierta
async function session(name) {
  if (!ort) {
    ort = await import("./ai/ort.wasm.min.mjs");
    ort.env.wasm.wasmPaths = new URL("./ai/", import.meta.url).href;
    ort.env.wasm.numThreads = 1;     // sin COOP/COEP no hay hilos; asi funciona en todos
  }
  if (!sessions[name]) {
    sessions[name] = await ort.InferenceSession.create(new URL("./ai/" + name + ".onnx", import.meta.url).href,
                                                       { executionProviders: ["wasm"] });
  }
  return sessions[name];
}
async function computeMatte(kind) {
  const cfg = AI[kind], n = cfg.size;
  // La foto sin ajustes, estirada a n x n (como se entrenaron los modelos)
  const px = ed.neutral_rgba(n * 2, n * 2), w = ed.out_width(), h = ed.out_height();
  const input = new Float32Array(3 * n * n);
  const mean = [0.485, 0.456, 0.406], std = [0.229, 0.224, 0.225];
  for (let y = 0; y < n; y++) {
    const fy = Math.min(h - 1.001, Math.max(0, (y + 0.5) * h / n - 0.5)), y0 = Math.floor(fy), ty = fy - y0;
    for (let x = 0; x < n; x++) {
      const fx = Math.min(w - 1.001, Math.max(0, (x + 0.5) * w / n - 0.5)), x0 = Math.floor(fx), tx = fx - x0;
      for (let c = 0; c < 3; c++) {
        const p = (yy, xx) => px[(yy * w + xx) * 4 + c];
        const v = (p(y0, x0) * (1 - tx) + p(y0, x0 + 1) * tx) * (1 - ty) + (p(y0 + 1, x0) * (1 - tx) + p(y0 + 1, x0 + 1) * tx) * ty;
        input[c * n * n + y * n + x] = (v / 255 - mean[c]) / std[c];
      }
    }
  }
  const sess = await session(cfg.model);
  const out = await sess.run({ [sess.inputNames[0]]: new ort.Tensor("float32", input, [1, 3, n, n]) });
  const y = out[sess.outputNames[0]].data;
  let lo = 0, hi = 1;
  if (cfg.minmax) { lo = Infinity; hi = -Infinity; for (const v of y) { if (v < lo) lo = v; if (v > hi) hi = v; } }
  const data = new Uint8Array(n * n);
  for (let i = 0; i < n * n; i++) {
    // un poco de contraste: lo dudoso hacia dentro o fuera, bordes suaves
    let t = (y[i] - lo) / Math.max(1e-6, hi - lo);
    t = Math.min(1, Math.max(0, (t - 0.2) / 0.6));
    data[i] = Math.round(t * t * (3 - 2 * t) * 255);
  }
  return { w: n, h: n, data };
}
// Las mattes que pide el estado (Sujeto/Fondo -> person, Cielo -> sky)
function neededMattes(st) {
  const need = new Set();
  for (const m of st.masks || []) for (const c of m.components || []) {
    const k = c.shape && c.shape.kind;
    if (k === "subject" || k === "background") need.add("person");
    if (k === "sky") need.add("sky");
  }
  return need;
}
const jobs = {};          // kind -> calculo en curso (una sola vez aunque lo pidan dos mensajes)
async function ensureMattes(kinds) {
  for (const k of kinds) {
    if (mattes[k]) continue;
    if (!jobs[k]) {
      const target = ed;
      jobs[k] = computeMatte(k).then((mt) => {
        if (ed === target) { mattes[k] = mt; ed.set_matte(k, mt.w, mt.h, mt.data); }
      }).finally(() => { delete jobs[k]; });
    }
    await jobs[k];
  }
}

// Lleva el estado del editor (lo que guarda la interfaz) al motor
function apply(e, st) {
  e.reset();
  // El perfil primero: su intensidad es un control mas (profile.amount)
  e.set_profile(st.profile || "lc.color", 100);
  for (const [id, v] of Object.entries(st.values || {})) e.set(id, v);
  e.set_bw(!!st.bw);
  e.set_orientation(st.orient || 1);
  const c = st.crop || [0, 0, 1, 1];
  e.set_crop(c[0], c[1], c[2], c[3], st.angle || 0);
  // Curvas por puntos: [[x, y], ...] por canal
  const cv = st.curves || {};
  for (const ch of ["master", "red", "green", "blue"]) {
    e.set_curve(ch, new Float64Array((cv[ch] || []).flat()));
  }
  // Capas de mascara (formato Mask de LightCraft) y correcciones (Spot)
  e.set_masks_json(JSON.stringify(st.masks || []));
  e.set_spots_json(JSON.stringify(st.spots || []));
}

async function fetchBytes(url, accept) {
  const res = await fetch(url, { headers: accept ? { Accept: accept } : {} });
  if (!res.ok) {
    let msg = "HTTP " + res.status;
    try { msg = (await res.json()).error || msg; } catch (e) { /* no era JSON */ }
    throw new Error(msg);
  }
  return { bytes: new Uint8Array(await res.arrayBuffer()), headers: res.headers };
}

self.onmessage = async (ev) => {
  const m = ev.data;
  try {
    await ready;
    if (m.type === "open") {
      if (ed) { ed.free(); ed = null; }
      mattes = {};
      srcBytes = null; srcUrl = null;
      if (m.source) {
        // El ORIGINAL (o la vista previa grande de un RAW), decodificado aqui con
        // los codecs de LightCraft a 2048 px: sin pasar por el WebP comprimido
        // del visor, que con sombras o curvas fuertes dejaba bandas y halos
        const { bytes, headers } = await fetchBytes(m.source);
        srcOrient = parseInt(headers.get("X-Orientation") || "1", 10) || 1;
        ed = new Editor(bytes, srcOrient, 2048);
        srcBytes = bytes; srcUrl = m.source;
      } else {
        // Formatos que el motor no abre (HEIC): la vista previa del servidor
        const { bytes } = await fetchBytes(m.url, "image/webp,image/*");
        ed = new Editor(bytes, 1, 2048);
      }
      self.postMessage({ type: "opened", seq: m.seq, w: ed.width(), h: ed.height(),
                         controls: JSON.parse(controls_json()), profiles: JSON.parse(profiles_json()) });
    } else if (m.type === "ai") {
      // Calcular ya (el editor ensena "Detectando..." mientras tanto)
      await ensureMattes(m.kinds);
      self.postMessage({ type: "ai", seq: m.seq });
    } else if (m.type === "spotSource") {
      apply(ed, m.st);
      const a = m.avoid;
      self.postMessage({ type: "spotSource", seq: m.seq,
                         offset: Array.from(ed.pick_spot_source(JSON.stringify(m.spot), a ? a[0] : 0, a ? a[1] : 0, !!a)) });
    } else if (m.type === "render") {
      await ensureMattes(neededMattes(m.st));
      apply(ed, m.st);
      ed.set_overlay(m.overlay == null ? -1 : m.overlay);
      const px = ed.render(m.maxW, m.maxH, !!m.draft, m.applyCrop !== false, !!m.before);
      const hist = ed.histogram();
      self.postMessage({ type: "rendered", seq: m.seq, w: ed.out_width(), h: ed.out_height(), px, hist },
                       [px.buffer, hist.buffer]);
    } else if (m.type === "auto") {
      apply(ed, m.st);
      self.postMessage({ type: "auto", seq: m.seq, values: JSON.parse(ed.auto_tone()) });
    } else if (m.type === "autoWb") {
      apply(ed, m.st);
      const [temp, tint] = ed.auto_wb();
      self.postMessage({ type: "autoWb", seq: m.seq, temp, tint });
    } else if (m.type === "sample") {
      apply(ed, m.st);
      self.postMessage({ type: "sample", seq: m.seq, lab: Array.from(ed.color_sample(m.x, m.y)) });
    } else if (m.type === "export") {
      // Resolucion completa: el original entero, revelado y codificado aqui.
      // Se suelta la vista previa mientras tanto para no duplicar memoria.
      let bytes = srcBytes, orient = srcOrient;
      if (!bytes || srcUrl !== m.url) {
        const r = await fetchBytes(m.url);
        bytes = r.bytes;
        orient = parseInt(r.headers.get("X-Orientation") || "1", 10) || 1;
      }
      // maxSide: en un movil sin memoria para la foto entera, revelarla mas pequena
      await ensureMattes(neededMattes(m.st));
      const full = new Editor(bytes, orient, m.maxSide || 0);
      try {
        for (const [k, mt] of Object.entries(mattes)) full.set_matte(k, mt.w, mt.h, mt.data);
        apply(full, m.st);
        const out = full.export(!!m.png, m.quality || 95);
        self.postMessage({ type: "exported", seq: m.seq, bytes: out, w: full.width(), h: full.height() },
                         [out.buffer]);
      } finally {
        full.free();
      }
    }
  } catch (e) {
    self.postMessage({ type: "error", seq: m.seq, msg: String(e && e.message || e) });
  }
};
