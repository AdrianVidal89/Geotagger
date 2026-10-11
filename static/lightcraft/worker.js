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
let ed = null;           // la foto de trabajo (vista previa de ~2048 px)

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
  // Capas de mascara (formato Mask de LightCraft)
  e.set_masks_json(JSON.stringify(st.masks || []));
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
      // La vista previa del servidor ya viene orientada (y suele estar en cache)
      const { bytes } = await fetchBytes(m.url, "image/webp,image/*");
      ed = new Editor(bytes, 1, 2048);
      self.postMessage({ type: "opened", seq: m.seq, w: ed.width(), h: ed.height(),
                         controls: JSON.parse(controls_json()), profiles: JSON.parse(profiles_json()) });
    } else if (m.type === "render") {
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
      const { bytes, headers } = await fetchBytes(m.url);
      const orient = parseInt(headers.get("X-Orientation") || "1", 10) || 1;
      // maxSide: en un movil sin memoria para la foto entera, revelarla mas pequena
      const full = new Editor(bytes, orient, m.maxSide || 0);
      try {
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
