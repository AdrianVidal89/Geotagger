// ── Editor LightCraft (aspecto Lightroom) ────────────────────────────────────
// Sustituye al editor clasico de Geotagger. El revelado lo hace el motor de
// LightCraft (https://github.com/storytold/lightcraft) compilado a
// WebAssembly, en un Worker de ESTE navegador: el NAS solo entrega la foto y
// recibe el JPEG terminado.
//
// Los deslizadores salen de la tabla de controles del propio motor (ids,
// rangos, valores por defecto, tipo de pista), asi que se comportan como en
// LightCraft. Aqui solo se ponen las etiquetas en espanol y el orden de
// Lightroom.
//
// Necesita del script principal: bust(), editedStamps y lcAfterSave().
(function () {
  "use strict";

  const BASE = "/static/lightcraft/";

  // Etiquetas de Lightroom en espanol
  const L = {
    "profile.amount": "Intensidad",
    "wb.tempRel": "Temperatura", "wb.tintRel": "Matiz",
    "light.exposure": "Exposición", "light.contrast": "Contraste", "light.highlights": "Iluminaciones",
    "light.shadows": "Sombras", "light.whites": "Blancos", "light.blacks": "Negros",
    "curve.highlights": "Iluminaciones", "curve.lights": "Claros", "curve.darks": "Oscuros", "curve.shadows": "Sombras",
    "color.vibrance": "Intensidad", "color.saturation": "Saturación",
    "grading.blending": "Fusión", "grading.balance": "Equilibrio",
    "effects.texture": "Textura", "effects.clarity": "Claridad", "effects.dehaze": "Neblina",
    "vignette.amount": "Viñeta", "vignette.midpoint": "Punto medio", "vignette.roundness": "Redondez",
    "vignette.feather": "Desvanecer", "vignette.highlights": "Iluminaciones",
    "grain.amount": "Granulado", "grain.size": "Tamaño", "grain.roughness": "Aspereza",
    "detail.sharpenAmount": "Enfoque", "detail.sharpenRadius": "Radio", "detail.sharpenDetail": "Detalle",
    "detail.sharpenMasking": "Máscara", "detail.nrLuminance": "Reducción de ruido", "detail.nrDetail": "Detalle",
    "detail.nrContrast": "Contraste", "detail.nrColor": "Ruido de color", "detail.nrColorDetail": "Detalle",
    "detail.nrColorSmoothness": "Suavidad",
    "optics.distortion": "Distorsión", "optics.vignetting": "Viñeteado", "optics.vignettingMidpoint": "Punto medio",
    "optics.defringePurple": "Desflecar morado", "optics.defringeGreen": "Desflecar verde",
    "optics.caRed": "Rojo/cian", "optics.caBlue": "Azul/amarillo",
    "geometry.vertical": "Vertical", "geometry.horizontal": "Horizontal", "geometry.rotate": "Girar",
    "geometry.aspect": "Aspecto", "geometry.scale": "Escala", "geometry.offsetX": "Desplazamiento X",
    "geometry.offsetY": "Desplazamiento Y",
    "calibration.shadowsTint": "Matiz de sombras", "calibration.redHue": "Rojo: tono", "calibration.redSat": "Rojo: saturación",
    "calibration.greenHue": "Verde: tono", "calibration.greenSat": "Verde: saturación",
    "calibration.blueHue": "Azul: tono", "calibration.blueSat": "Azul: saturación",
    "crop.angle": "Ángulo",
  };
  const BANDS = [
    ["red", "Rojo", "#e04040"], ["orange", "Naranja", "#e8892e"], ["yellow", "Amarillo", "#e2cf38"],
    ["green", "Verde", "#45b045"], ["aqua", "Aguamarina", "#30c0c0"], ["blue", "Azul", "#3a62e0"],
    ["purple", "Morado", "#8a44d0"], ["magenta", "Magenta", "#d040b0"],
  ];
  const PROFILES = {
    "lc.color": "Color (sin perfil)", "lc.neutral": "Neutro", "lc.vivid": "Vivo", "lc.landscape": "Paisaje",
    "lc.portrait": "Retrato", "lc.mono": "Monocromo", "lc.film.warm-print": "Película · Copia cálida",
    "lc.film.cool-fade": "Película · Fundido frío", "lc.film.golden-hour": "Película · Hora dorada",
    "lc.film.faded-slide": "Película · Diapositiva", "lc.cine.teal-amber": "Cine · Turquesa y ámbar",
    "lc.cine.night-blue": "Cine · Noche azul", "lc.cine.desert-heat": "Cine · Desierto",
    "lc.cine.neon-dusk": "Cine · Neón", "lc.muted.matte-soft": "Suave · Mate",
    "lc.muted.bleached": "Suave · Blanqueado", "lc.muted.pastel-haze": "Suave · Pastel",
    "lc.muted.quiet-green": "Suave · Verde tranquilo", "lc.bw.mono-rich": "B/N · Intenso",
    "lc.bw.red-filter": "B/N · Filtro rojo", "lc.bw.soft": "B/N · Suave", "lc.bw.sepia": "B/N · Sepia",
  };
  // Composicion de giros sobre valores EXIF de orientacion
  const OPS = {
    cw:    {1:6, 2:7, 3:8, 4:5, 5:2, 6:3, 7:4, 8:1},
    ccw:   {1:8, 2:5, 3:6, 4:7, 5:4, 6:1, 7:2, 8:3},
    fliph: {1:2, 2:1, 3:4, 4:3, 5:6, 6:5, 7:8, 8:7},
    flipv: {1:4, 2:3, 3:2, 4:1, 5:8, 6:7, 7:6, 8:5},
  };
  // Lo mismo, como parametros de la via SIN PERDIDA de /api/edit
  const ORIENT_PARAMS = {
    1: {rotate: 0, flip_h: false, flip_v: false}, 2: {rotate: 0, flip_h: true, flip_v: false},
    3: {rotate: 0, flip_h: true, flip_v: true},   4: {rotate: 0, flip_h: false, flip_v: true},
    5: {rotate: 90, flip_h: true, flip_v: false}, 6: {rotate: 90, flip_h: false, flip_v: false},
    7: {rotate: 90, flip_h: false, flip_v: true}, 8: {rotate: 90, flip_h: true, flip_v: true},
  };
  const ICON = {
    close: "M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z",
    undo: "M12.5 8c-2.65 0-5.05.99-6.9 2.6L2 7v9h9l-3.62-3.62c1.39-1.16 3.16-1.88 5.12-1.88 3.54 0 6.55 2.31 7.6 5.5l2.37-.78C21.08 11.03 17.15 8 12.5 8z",
    redo: "M18.4 10.6C16.55 8.99 14.15 8 11.5 8c-4.65 0-8.58 3.03-9.96 7.22L3.9 16c1.05-3.19 4.05-5.5 7.6-5.5 1.95 0 3.73.72 5.12 1.88L13 16h9V7l-3.6 3.6z",
    ba: "M10 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h5v2h2V1h-2v2zm0 15H5l5-6v6zm9-15h-5v2h5v13l-5-6v9h5c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2z",
    reset: "M12 5V1L7 6l5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z",
    edit: "M3 17v2h6v-2H3zM3 5v2h10V5H3zm10 16v-2h8v-2h-8v-2h-2v6h2zM7 9v2H3v2h4v2h2V9H7zm14 4v-2H11v2h10zm-6-4h2V7h4V5h-4V3h-2v6z",
    crop: "M17 15h2V7c0-1.1-.9-2-2-2H9v2h8v8zM7 17V1H5v4H1v2h4v10c0 1.1.9 2 2 2h10v4h2v-4h4v-2H7z",
    ccw: "M7.11 8.53L5.7 7.11C4.8 8.27 4.24 9.61 4.07 11h2.02c.14-.87.49-1.72 1.02-2.47zM6.09 13H4.07c.17 1.39.72 2.73 1.62 3.89l1.41-1.42c-.52-.75-.87-1.59-1.01-2.47zm1.01 5.32c1.16.9 2.51 1.44 3.9 1.61V17.9c-.87-.15-1.71-.49-2.46-1.03L7.1 18.32zM13 4.07V1L8.45 5.55 13 10V6.09c2.84.48 5 2.94 5 5.91s-2.16 5.43-5 5.91v2.02c3.95-.49 7-3.85 7-7.93s-3.05-7.44-7-7.93z",
    cw: "M15.55 5.55L11 1v3.07C7.06 4.56 4 7.92 4 12s3.05 7.44 7 7.93v-2.02c-2.84-.48-5-2.94-5-5.91s2.16-5.43 5-5.91V10l4.55-4.45zM19.93 11c-.17-1.39-.72-2.73-1.62-3.89l-1.42 1.42c.54.75.88 1.6 1.02 2.47h2.02zM13 17.9v2.02c1.39-.17 2.74-.71 3.9-1.61l-1.44-1.44c-.75.54-1.59.89-2.46 1.03zm3.89-2.42l1.42 1.41c.9-1.16 1.45-2.5 1.62-3.89h-2.02c-.14.87-.48 1.72-1.02 2.48z",
  };
  Object.assign(ICON, {
    auto: "M7.5 5.6L10 7 8.6 4.5 10 2 7.5 3.4 5 2l1.4 2.5L5 7zm12 9.8L17 14l1.4 2.5L17 19l2.5-1.4L22 19l-1.4-2.5L22 14zM22 2l-2.5 1.4L17 2l1.4 2.5L17 7l2.5-1.4L22 7l-1.4-2.5zm-7.63 5.29a.996.996 0 0 0-1.41 0L1.29 18.96a.996.996 0 0 0 0 1.41l2.34 2.34c.39.39 1.02.39 1.41 0L16.7 11.05a.996.996 0 0 0 0-1.41l-2.33-2.35zm-1.03 5.49l-2.12-2.12 2.44-2.44 2.12 2.12-2.44 2.44z",
    profile: "M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-1 9h-4v4h-2v-4H9V9h4V5h2v4h4v2z",
    light: "M20 8.69V4h-4.69L12 .69 8.69 4H4v4.69L.69 12 4 15.31V20h4.69L12 23.31 15.31 20H20v-4.69L23.31 12 20 8.69zM12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6 6 2.69 6 6-2.69 6-6 6zm0-10c-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4-1.79-4-4-4z",
    color: "M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z",
    effects: "M19 9l1.25-2.75L23 5l-2.75-1.25L19 1l-1.25 2.75L15 5l2.75 1.25zM11.5 9.5L9 4 6.5 9.5 1 12l5.5 2.5L9 20l2.5-5.5L17 12z",
    detail: "M12 2l-5.5 9h11L12 2zm5.5 11c-2.49 0-4.5 2.01-4.5 4.5s2.01 4.5 4.5 4.5 4.5-2.01 4.5-4.5-2.01-4.5-4.5-4.5zM3 21.5h8v-8H3v8z",
    optics: "M12 15.2A3.2 3.2 0 1 0 12 8.8a3.2 3.2 0 0 0 0 6.4zM9 2L7.17 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2h-3.17L15 2H9z",
    geometry: "M21 3H3v18h18V3zm-2 16H5V5h14v14zM7 7h2v2H7zm4 0h2v2h-2zm4 0h2v2h-2zM7 11h2v2H7zm4 0h2v2h-2zm4 0h2v2h-2zM7 15h2v2H7zm4 0h2v2h-2zm4 0h2v2h-2z",
    mask: "M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18V4c4.41 0 8 3.59 8 8s-3.59 8-8 8z",
  });
  const escH = (t) => String(t == null ? "" : t).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  // Barra inferior en el movil (como Lightroom en el iPhone)
  const GROUPS = [["auto", "Auto"], ["profile", "Perfil"], ["light", "Luz"], ["color", "Color"], ["effects", "Efectos"],
                  ["detail", "Detalle"], ["optics", "Óptica"], ["geometry", "Geometría"], ["masks", "Máscaras"], ["crop", "Recortar"]];
  const svg = (d) => '<svg viewBox="0 0 24 24"><path d="' + d + '"/></svg>';

  // ── Estado ─────────────────────────────────────────────────────────────────
  let el = null, worker = null, seq = 0;
  let specs = {};                // id -> ControlSpec de LightCraft
  let file = null;               // la foto ({path, name, ext...})
  let W = 0, H = 0;              // tamano de la foto de trabajo (orientada)
  let st = null;                 // lo editable (va entero al motor)
  let undo = [], redo = [];
  let tool = "edit", before = false, zoom = false, saving = false;
  let gradeZone = "shadows", mixTab = "hue", mixBand = 0;
  const pending = {};            // seq -> {resolve, reject}
  let panelBuilt = false;

  function freshState() {
    return { values: {}, bw: false, profile: "lc.color",
             orient: 1, crop: [0, 0, 1, 1], angle: 0, aspect: 0,
             curves: { master: [], red: [], green: [], blue: [] }, masks: [] };
  }
  const snap = () => JSON.stringify(st);
  const isDefaultCrop = () => st.angle === 0 && st.crop.join() === "0,0,1,1";
  function hasPixelEdits() {
    return Object.keys(st.values).length > 0 || st.bw || st.profile !== "lc.color" || !isDefaultCrop() || curvesTouched() || masksTouched();
  }
  const hasChanges = () => hasPixelEdits() || st.orient !== 1;

  // ── Worker ─────────────────────────────────────────────────────────────────
  function call(msg, transfer) {
    return new Promise((resolve, reject) => {
      msg.seq = ++seq;
      pending[msg.seq] = { resolve, reject };
      worker.postMessage(msg, transfer || []);
    });
  }
  function startWorker() {
    if (worker) return;
    worker = new Worker(BASE + "worker.js", { type: "module" });
    worker.onmessage = (ev) => {
      const m = ev.data, p = pending[m.seq];
      if (!p) return;
      delete pending[m.seq];
      if (m.type === "error") p.reject(new Error(m.msg)); else p.resolve(m);
    };
    worker.onerror = (e) => {
      // Un fallo dentro del motor deja el Worker inservible: se recrea
      for (const k of Object.keys(pending)) { pending[k].reject(new Error(e.message || "fallo del motor")); delete pending[k]; }
      worker.terminate();
      worker = null;
    };
  }

  // ── Revelado de la vista (el ultimo pedido gana) ───────────────────────────
  let rendering = false, wanted = null;
  function requestRender(draft) {
    wanted = { draft: !!draft };
    if (!rendering) pump();
  }
  async function pump() {
    if (!wanted || !worker || !file) return;
    const o = wanted;
    wanted = null;
    rendering = true;
    const stage = el.querySelector(".lc-stage");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    let mw = zoom ? 4096 : Math.round((stage.clientWidth - 32) * dpr);
    let mh = zoom ? 4096 : Math.round((stage.clientHeight - 32) * dpr);
    if (o.draft) { mw = Math.round(mw * 0.6); mh = Math.round(mh * 0.6); }
    try {
      const m = curMask();
      const r = await call({ type: "render", st: st, maxW: Math.max(64, mw), maxH: Math.max(64, mh),
                             draft: o.draft, applyCrop: tool !== "crop", before: before,
                             overlay: tool === "mask" && overlayOn && m && m.components.length ? m.id : -1 });
      draw(r);
    } catch (e) {
      showMsg("No se pudo revelar: " + e.message);
      startWorker();
    }
    rendering = false;
    if (wanted) pump();
  }

  function draw(r) {
    const cv = el.querySelector(".lc-canvas");
    if (cv.width !== r.w || cv.height !== r.h) { cv.width = r.w; cv.height = r.h; }
    cv.getContext("2d").putImageData(new ImageData(new Uint8ClampedArray(r.px.buffer, r.px.byteOffset, r.px.byteLength), r.w, r.h), 0, 0);
    if (zoom) {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      cv.style.width = (r.w / dpr) + "px";
      cv.style.height = (r.h / dpr) + "px";
    } else {
      cv.style.width = cv.style.height = "";
    }
    lastHist = r.hist;
    drawHistogram(r.hist);
    drawCurve();
    applyTilt();
    el.querySelector(".lc-badge").classList.toggle("on", before);
    if (tool === "crop") layoutCrop();
    if (tool === "mask") layoutMaskLayer();
  }

  function drawHistogram(h) {
    const cv = el.querySelector(".lc-hist canvas");
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth * dpr, hh = cv.clientHeight * dpr;
    if (cv.width !== w || cv.height !== hh) { cv.width = w; cv.height = hh; }
    const g = cv.getContext("2d");
    g.clearRect(0, 0, w, hh);
    let max = 1;
    for (let c = 0; c < 3; c++) for (let i = 2; i < 254; i++) max = Math.max(max, h[c * 256 + i]);
    g.globalCompositeOperation = "lighter";
    ["rgba(220,60,60,.75)", "rgba(60,200,80,.75)", "rgba(70,110,235,.8)"].forEach((col, c) => {
      g.fillStyle = col;
      g.beginPath();
      g.moveTo(0, hh);
      for (let i = 0; i < 256; i++) {
        const v = Math.min(1, Math.sqrt(h[c * 256 + i] / max));
        g.lineTo(i / 255 * w, hh - v * hh * 0.95);
      }
      g.lineTo(w, hh);
      g.closePath();
      g.fill();
    });
    g.globalCompositeOperation = "source-over";
  }

  // ── Cambios y deshacer ─────────────────────────────────────────────────────
  function commit() {
    const s = snap();
    if (undo[undo.length - 1] !== s) { undo.push(s); redo = []; }
    if (undo.length > 200) undo.shift();
    refreshChrome();
  }
  function restore(s) {
    st = JSON.parse(s);
    if (!st.curves) st.curves = { master: [], red: [], green: [], blue: [] };
    if (!st.masks) st.masks = [];
    renderMaskPanel();
    syncControls();
    drawCurve();
    refreshChrome();
    requestRender(false);
  }
  function doUndo() { if (undo.length > 1) { redo.push(undo.pop()); restore(undo[undo.length - 1]); } }
  function doRedo() { if (redo.length) { const s = redo.pop(); undo.push(s); restore(s); } }

  const isMaskId = (id) => /^(mask|cmp|brush)\./.test(id);
  function getVal(id) {
    if (id === "crop.angle") return st.angle;
    if (isMaskId(id)) return maskGet(id);
    if (id in st.values) return st.values[id];
    const sp = specs[id];
    return sp ? sp.default : 0;
  }
  function setVal(id, v) {
    const sp = specs[id];
    if (sp) v = Math.min(sp.max, Math.max(sp.min, v));
    if (isMaskId(id)) { maskSet(id, v); return; }
    if (sp && Math.abs(v - sp.default) < 1e-9) delete st.values[id]; else st.values[id] = v;
  }

  function refreshChrome() {
    if (!panelBuilt) return;
    el.querySelector('[data-a="undo"]').disabled = undo.length <= 1;
    el.querySelector('[data-a="redo"]').disabled = redo.length === 0;
    el.querySelector('[data-a="save"]').disabled = !hasChanges() || saving;
    el.querySelector('[data-a="bw"]').classList.toggle("on", st.bw);
    el.querySelector(".lc-sel-profile").value = st.profile;
    el.querySelector(".lc-prof-amt").style.display = st.profile === "lc.color" ? "none" : "";
    el.querySelectorAll(".lc-only-bw").forEach(n => n.style.display = st.bw ? "" : "none");
    el.querySelectorAll(".lc-only-color").forEach(n => n.style.display = st.bw ? "none" : "");
    // Punto azul en las secciones tocadas
    el.querySelectorAll(".lc-sec").forEach(sec => {
      const ids = Array.from(sec.querySelectorAll(".lc-sl")).map(n => n.dataset.id).concat((sec.dataset.ids || "").split(",").filter(Boolean));
      sec.classList.toggle("mod", ids.some(id => id in st.values) || (sec.dataset.sec === "light" && curvesTouched()));
    });
  }

  // ── Deslizadores ───────────────────────────────────────────────────────────
  function trackCss(sp) {
    const t = sp.track || { kind: "plain" };
    const band = (i) => BANDS[(i + 8) % 8][2];
    switch (t.kind) {
      case "temp": return "linear-gradient(90deg,#3d6fd6,#c9c9c9,#e0b83c)";
      case "tint": return "linear-gradient(90deg,#3cad48,#c9c9c9,#c446b8)";
      case "gradient": return "linear-gradient(90deg," + t.from + "," + t.to + ")";
      case "hue": return "linear-gradient(90deg," + band(t.band - 1) + "," + band(t.band) + "," + band(t.band + 1) + ")";
      case "sat": return "linear-gradient(90deg,#808080," + band(t.band) + ")";
      case "lum": return "linear-gradient(90deg,#1a1a1a," + band(t.band) + ",#f2f2f2)";
      case "rainbow": return "linear-gradient(90deg,#f33,#fb3,#ef3,#3e5,#3ce,#36f,#c3f,#f33)";
      default: return "#5a5a5a";
    }
  }
  function fmt(sp, v) {
    const s = Number(v).toFixed(sp.decimals || 0);
    return (v > 0 && sp.min < 0) ? "+" + s : s;
  }
  // Un deslizador ligado a un control. `label` sustituye a la etiqueta.
  function slider(id, label) {
    const sp = specs[id];
    if (!sp) return "";
    return '<div class="lc-sl" data-id="' + id + '">' +
      '<div class="lc-sl-top"><label>' + (label || L[id] || sp.label) + '</label>' +
      '<input class="lc-val" type="text" inputmode="decimal"></div>' +
      '<input class="lc-range" type="range" min="' + sp.min + '" max="' + sp.max + '" step="' + sp.step + '"' +
      ' style="--lc-track:' + trackCss(sp) + '"></div>';
  }
  function syncSlider(n) {
    const id = n.dataset.id, sp = specs[id];
    if (!sp) return;
    const v = getVal(id);
    n.querySelector(".lc-range").value = v;
    const box = n.querySelector(".lc-val");
    if (document.activeElement !== box) box.value = fmt(sp, v);
    n.classList.toggle("mod", id === "crop.angle" ? st.angle !== 0 : isMaskId(id) ? Math.abs(v - sp.default) > 1e-9 : id in st.values);
  }
  function syncControls() {
    if (!panelBuilt) return;
    el.querySelectorAll(".lc-sl").forEach(syncSlider);
    el.querySelector(".lc-sel-profile").value = st.profile;
    el.querySelectorAll("[data-aspect]").forEach(b => {
      const a = +b.dataset.aspect;
      b.classList.toggle("on", a === st.aspect || (a > 0 && Math.abs(1 / a - st.aspect) < 1e-3));
    });
  }
  // Ids dinamicos: la mezcla de color y la gradacion cambian de banda/zona
  function rebindDynamic() {
    const mix = el.querySelector(".lc-mix-sliders");
    const b = BANDS[mixBand][0];
    mix.innerHTML = slider("mixer." + b + ".hue", "Tono") + slider("mixer." + b + ".sat", "Saturación") + slider("mixer." + b + ".lum", "Luminancia");
    el.querySelectorAll(".lc-mix-dots .lc-dotb").forEach((d, i) => d.classList.toggle("on", i === mixBand));
    const gz = el.querySelector(".lc-grade-sliders");
    gz.innerHTML = slider("grading." + gradeZone + ".hue", "Tono") + slider("grading." + gradeZone + ".sat", "Saturación") +
                   slider("grading." + gradeZone + ".lum", "Luminancia");
    el.querySelectorAll("[data-zone]").forEach(z => z.classList.toggle("on", z.dataset.zone === gradeZone));
    el.querySelectorAll(".lc-mix-sliders .lc-sl, .lc-grade-sliders .lc-sl").forEach(syncSlider);
  }

  // ── Curva de tonos (Maestro, Rojo, Verde, Azul) ───────────────────────────
  // La curva por puntos del motor: pares (entrada, salida) 0..1 sobre la
  // imagen ya codificada, interpolados con la misma Hermite monotona
  // (Fritsch-Carlson) que usa LightCraft, asi que lo que se dibuja es
  // exactamente lo que se aplica. Clic sobre la curva: punto nuevo. Arrastrar
  // un punto: moverlo. Sacarlo del recuadro o doble clic: quitarlo.
  const CURVE_CH = [["master", "Maestro", "#e6e6e6"], ["red", "Rojo", "#ff4d4d"],
                    ["green", "Verde", "#3ccf5a"], ["blue", "Azul", "#4d84ff"]];
  let curveCh = "master", lastHist = null;
  const pairs = (flat) => { const o = []; for (let i = 0; i + 1 < flat.length; i += 2) o.push([flat[i], flat[i + 1]]); return o; };
  const isIdentity = (p) => p.length < 2 || p.every(q => Math.abs(q[0] - q[1]) < 1e-6);
  function curvesTouched() {
    const c = st && st.curves;
    return !!c && ["master", "red", "green", "blue"].some(k => !isIdentity(c[k] || []));
  }
  function curvePts(ch) {
    const p = (st.curves[ch] || []).slice().sort((a, b) => a[0] - b[0]);
    return p.length >= 2 ? p : [[0, 0], [1, 1]];
  }
  function monotone(pts) {
    const n = pts.length, xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
    const d = [];
    for (let i = 0; i < n - 1; i++) d.push((ys[i + 1] - ys[i]) / Math.max(1e-9, xs[i + 1] - xs[i]));
    const ms = new Array(n).fill(0);
    ms[0] = d[0]; ms[n - 1] = d[n - 2];
    for (let i = 1; i < n - 1; i++) ms[i] = d[i - 1] * d[i] <= 0 ? 0 : (d[i - 1] + d[i]) / 2;
    for (let i = 0; i < n - 1; i++) {
      if (d[i] === 0) { ms[i] = 0; ms[i + 1] = 0; continue; }
      const a = ms[i] / d[i], b = ms[i + 1] / d[i], s2 = a * a + b * b;
      if (s2 > 9) { const t = 3 / Math.sqrt(s2); ms[i] = t * a * d[i]; ms[i + 1] = t * b * d[i]; }
    }
    return (x) => {
      if (x <= xs[0]) return ys[0];
      if (x >= xs[n - 1]) return ys[n - 1];
      let i = 0;
      while (i < n - 2 && x > xs[i + 1]) i++;
      const h = xs[i + 1] - xs[i], t = (x - xs[i]) / h, t2 = t * t, t3 = t2 * t;
      return (2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h * ms[i] +
             (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h * ms[i + 1];
    };
  }
  function curveHtml() {
    return '<div class="lc-sub">Curva de tonos<button class="lc-btn sm" data-a="curvereset" title="Restablecer este canal">Restablecer</button></div>' +
      '<div class="lc-curve-ch">' + CURVE_CH.map(c =>
        '<button class="lc-cch" data-ch="' + c[0] + '" title="' + c[1] + '"><span style="background:' + c[2] + '"></span>' + c[1] + '</button>').join("") + '</div>' +
      '<div class="lc-curve"><canvas></canvas><div class="lc-curve-read"></div></div>' +
      '<details class="lc-param"><summary>Curva paramétrica</summary>' +
        many(["curve.highlights", "curve.lights", "curve.darks", "curve.shadows"]) + '</details>';
  }
  function drawCurve() {
    if (!panelBuilt) return;
    const cv = el.querySelector(".lc-curve canvas");
    if (!cv || !cv.clientWidth) return;
    const dpr = window.devicePixelRatio || 1, S = cv.clientWidth * dpr;
    if (cv.width !== S || cv.height !== S) { cv.width = S; cv.height = S; }
    const g = cv.getContext("2d"), pad = 6 * dpr, w = S - 2 * pad;
    const X = (x) => pad + x * w, Y = (y) => pad + (1 - y) * w;
    g.clearRect(0, 0, S, S);
    g.fillStyle = "#1a1a1a"; g.fillRect(pad, pad, w, w);
    // histograma del canal (luminancia para el maestro), de fondo
    if (lastHist) {
      const off = { red: 0, green: 256, blue: 512, master: 768 }[curveCh];
      let max = 1;
      for (let i = 2; i < 254; i++) max = Math.max(max, lastHist[off + i]);
      g.fillStyle = curveCh === "master" ? "rgba(255,255,255,.13)" :
        { red: "rgba(255,77,77,.22)", green: "rgba(60,207,90,.2)", blue: "rgba(77,132,255,.24)" }[curveCh];
      g.beginPath(); g.moveTo(X(0), Y(0));
      for (let i = 0; i < 256; i++) g.lineTo(X(i / 255), Y(Math.min(1, Math.sqrt(lastHist[off + i] / max)) * 0.9));
      g.lineTo(X(1), Y(0)); g.closePath(); g.fill();
    }
    // cuadricula en cuartos y diagonal
    g.strokeStyle = "#3a3a3a"; g.lineWidth = dpr;
    g.beginPath();
    for (let k = 1; k < 4; k++) { g.moveTo(X(k / 4), Y(0)); g.lineTo(X(k / 4), Y(1)); g.moveTo(X(0), Y(k / 4)); g.lineTo(X(1), Y(k / 4)); }
    g.stroke();
    g.strokeStyle = "#555"; g.setLineDash([3 * dpr, 3 * dpr]);
    g.beginPath(); g.moveTo(X(0), Y(0)); g.lineTo(X(1), Y(1)); g.stroke(); g.setLineDash([]);
    // los otros canales tocados, tenues (como Lightroom)
    for (const [ch, , col] of CURVE_CH) {
      if (ch === curveCh || isIdentity(st.curves[ch] || [])) continue;
      const f = monotone(curvePts(ch));
      g.strokeStyle = col; g.globalAlpha = 0.35; g.lineWidth = 1.2 * dpr;
      g.beginPath();
      for (let i = 0; i <= 200; i++) { const x = i / 200; i ? g.lineTo(X(x), Y(f(x))) : g.moveTo(X(x), Y(f(x))); }
      g.stroke(); g.globalAlpha = 1;
    }
    const pts = curvePts(curveCh), f = monotone(pts), col = CURVE_CH.find(c => c[0] === curveCh)[2];
    g.strokeStyle = col; g.lineWidth = 2 * dpr;
    g.beginPath();
    for (let i = 0; i <= 256; i++) { const x = i / 256; i ? g.lineTo(X(x), Y(f(x))) : g.moveTo(X(x), Y(f(x))); }
    g.stroke();
    for (const [i, p] of pts.entries()) {
      g.beginPath(); g.arc(X(p[0]), Y(p[1]), (i === curveDragIdx ? 6 : 4.5) * dpr, 0, Math.PI * 2);
      g.fillStyle = i === curveDragIdx ? "#fff" : "#1a1a1a"; g.fill();
      g.strokeStyle = col; g.lineWidth = 1.6 * dpr; g.stroke();
    }
    el.querySelectorAll(".lc-cch").forEach(b => {
      b.classList.toggle("on", b.dataset.ch === curveCh);
      b.classList.toggle("mod", !isIdentity(st.curves[b.dataset.ch] || []));
    });
  }
  let curveDragIdx = -1;
  function wireCurve() {
    const panel = el.querySelector(".lc-panel");
    panel.addEventListener("click", (e) => {
      const b = e.target.closest(".lc-cch");
      if (b) { curveCh = b.dataset.ch; drawCurve(); }
    });
    const pos = (cv, e) => {
      const r = cv.getBoundingClientRect(), pad = 6, w = r.width - 2 * pad;
      return [(e.clientX - r.left - pad) / w, 1 - (e.clientY - r.top - pad) / w];
    };
    const store = (pts) => { st.curves[curveCh] = isIdentity(pts) && pts.length <= 2 ? [] : pts.map(p => [+p[0].toFixed(4), +p[1].toFixed(4)]); };
    let drag = null;
    panel.addEventListener("pointerdown", (e) => {
      const cv = e.target.closest(".lc-curve canvas");
      if (!cv) return;
      e.preventDefault();
      const [x, y] = pos(cv, e), pts = curvePts(curveCh);
      const tol = 12 / cv.getBoundingClientRect().width;
      let idx = pts.findIndex(p => Math.abs(p[0] - x) < tol && Math.abs(p[1] - y) < tol);
      if (idx < 0) {
        // punto nuevo sobre la curva, en la x del clic
        const cx = Math.min(0.99, Math.max(0.01, x));
        if (pts.some(p => Math.abs(p[0] - cx) < 0.02) || pts.length >= 16) return;
        const ny = Math.min(1, Math.max(0, Math.abs(y - monotone(pts)(cx)) < 0.08 ? monotone(pts)(cx) : y));
        pts.push([cx, ny]); pts.sort((a, b) => a[0] - b[0]);
        idx = pts.findIndex(p => p[0] === cx);
        store(pts);
      }
      cv.setPointerCapture(e.pointerId);
      drag = { cv, idx, out: false };
      curveDragIdx = idx;
      drawCurve();
      requestRender(true);
    });
    panel.addEventListener("pointermove", (e) => {
      const cv = e.target.closest(".lc-curve canvas") || (drag && drag.cv);
      if (!cv) return;
      const [x, y] = pos(cv, e);
      const read = el.querySelector(".lc-curve-read");
      if (!drag) {
        if (x >= 0 && x <= 1 && y >= 0 && y <= 1) read.textContent = "Entrada " + Math.round(x * 255) + " → salida " + Math.round(monotone(curvePts(curveCh))(x) * 255);
        return;
      }
      const pts = curvePts(curveCh), i = drag.idx, n = pts.length;
      // Fuera del recuadro: el punto se quita al soltar (no los extremos)
      drag.out = i > 0 && i < n - 1 && (x < -0.08 || x > 1.08 || y < -0.08 || y > 1.08);
      const lo = i === 0 ? 0 : pts[i - 1][0] + 0.01, hi = i === n - 1 ? 1 : pts[i + 1][0] - 0.01;
      pts[i] = [Math.min(hi, Math.max(lo, x)), Math.min(1, Math.max(0, y))];
      store(pts);
      read.textContent = drag.out ? "Suelta para quitar el punto" :
        "Entrada " + Math.round(pts[i][0] * 255) + " → salida " + Math.round(pts[i][1] * 255);
      drawCurve();
      requestRender(true);
    });
    const end = () => {
      if (!drag) return;
      if (drag.out) { const pts = curvePts(curveCh); pts.splice(drag.idx, 1); store(pts); }
      drag = null; curveDragIdx = -1;
      drawCurve(); commit(); requestRender(false);
    };
    panel.addEventListener("pointerup", end);
    panel.addEventListener("pointercancel", end);
    panel.addEventListener("dblclick", (e) => {
      const cv = e.target.closest(".lc-curve canvas");
      if (!cv) return;
      e.stopPropagation();
      const [x, y] = pos(cv, e), pts = curvePts(curveCh), tol = 12 / cv.getBoundingClientRect().width;
      const idx = pts.findIndex(p => Math.abs(p[0] - x) < tol && Math.abs(p[1] - y) < tol);
      if (idx > 0 && idx < pts.length - 1) pts.splice(idx, 1);   // doble clic en un punto: quitarlo
      else if (idx < 0) pts.splice(0, pts.length);                // en el fondo: canal a cero
      store(pts.length >= 2 ? pts : []);
      drawCurve(); commit(); requestRender(false);
    }, true);
  }

  // ── Máscaras (capas con ajustes locales) ───────────────────────────────────
  // El modelo es el de LightCraft (`Mask`): cada capa tiene componentes que se
  // suman o se restan (pincel, degradado lineal, degradado radial, rango de
  // luminancia, rango de color, y cielo/sujeto/fondo automaticos) y sus propios
  // ajustes. Las coordenadas van normalizadas al marco de la foto ya girada y
  // SIN recortar (el mismo del recorte): aqui se pasan a la vista y vuelta.
  const SHAPES = [
    ["subject", "Sujeto", "Detección automática del motivo principal"],
    ["sky", "Cielo", "Detección automática del cielo"],
    ["background", "Fondo", "Todo menos el sujeto"],
    ["brush", "Pincel", "Pinta donde quieras el ajuste"],
    ["linear", "Degradado lineal", "Arrastra sobre la foto para trazarlo"],
    ["radial", "Degradado radial", "Arrastra sobre la foto para trazarlo"],
    ["colorRange", "Rango de color", "Toca un color de la foto"],
    ["luminanceRange", "Rango de luminancia", "Elige qué tonos"],
  ];
  const SHAPE_NAME = Object.fromEntries(SHAPES.map(s => [s[0], s[1]]));
  // Ajustes locales (LocalAdjustments de LightCraft)
  const LOCAL = [
    ["amount", "Cantidad", 0, 200, 100, 1, 0, { kind: "plain" }],
    ["temp", "Temperatura", -100, 100, 0, 1, 0, { kind: "temp" }],
    ["tint", "Matiz", -100, 100, 0, 1, 0, { kind: "tint" }],
    ["exposure", "Exposición", -4, 4, 0, 0.01, 2, { kind: "centered" }],
    ["contrast", "Contraste", -100, 100, 0, 1, 0, { kind: "centered" }],
    ["highlights", "Iluminaciones", -100, 100, 0, 1, 0, { kind: "centered" }],
    ["shadows", "Sombras", -100, 100, 0, 1, 0, { kind: "centered" }],
    ["whites", "Blancos", -100, 100, 0, 1, 0, { kind: "centered" }],
    ["blacks", "Negros", -100, 100, 0, 1, 0, { kind: "centered" }],
    ["texture", "Textura", -100, 100, 0, 1, 0, { kind: "centered" }],
    ["clarity", "Claridad", -100, 100, 0, 1, 0, { kind: "centered" }],
    ["dehaze", "Neblina", -100, 100, 0, 1, 0, { kind: "centered" }],
    ["hue", "Tono", -100, 100, 0, 1, 0, { kind: "rainbow" }],
    ["saturation", "Saturación", -100, 100, 0, 1, 0, { kind: "gradient", from: "#7a7a7a", to: "#e04a3a" }],
    ["sharpness", "Enfoque", -100, 100, 0, 1, 0, { kind: "centered" }],
    ["noise", "Ruido", -100, 100, 0, 1, 0, { kind: "centered" }],
  ];
  // Parametros de la forma elegida y del pincel
  const CMP = [
    ["cmp.feather", "Desvanecer", 0, 100, 50, 1, 0],
    ["cmp.lo", "Desde", 0, 100, 0, 1, 0], ["cmp.hi", "Hasta", 0, 100, 50, 1, 0],
    ["cmp.loFeather", "Suavizado oscuros", 0, 100, 10, 1, 0], ["cmp.hiFeather", "Suavizado claros", 0, 100, 10, 1, 0],
    ["cmp.refine", "Refinar", 0, 100, 50, 1, 0],
    ["brush.size", "Tamaño", 1, 100, 12, 1, 0], ["brush.feather", "Desvanecer", 0, 100, 50, 1, 0],
    ["brush.flow", "Flujo", 1, 100, 100, 1, 0],
  ];
  function registerMaskSpecs() {
    for (const [k, label, min, max, def, step, dec, track] of LOCAL)
      specs["mask." + k] = { id: "mask." + k, label, min, max, default: def, step, decimals: dec, track };
    for (const [id, label, min, max, def, step, dec] of CMP)
      specs[id] = { id, label, min, max, default: def, step, decimals: dec, track: { kind: "plain" } };
  }
  let selMask = -1, selComp = 0, overlayOn = true, maskMenuFor = null;
  const brush = { size: 12, feather: 50, flow: 100, erase: false };
  const curMask = () => (st.masks || [])[selMask] || null;
  const curComp = () => { const m = curMask(); return m ? m.components[selComp] || null : null; };
  const masksTouched = () => (st.masks || []).some(m => m.components.length > 0);
  function newShape(kind) {
    switch (kind) {
      case "linear": return { kind, start: { x: 0.5, y: 0.25 }, end: { x: 0.5, y: 0.6 } };
      case "radial": return { kind, center: { x: 0.5, y: 0.5 }, rx: 0.25, ry: 0.18, angle: 0, feather: 50, invert: false };
      case "brush": return { kind, strokes: [] };
      case "colorRange": return { kind, samples: [], refine: 50 };
      case "luminanceRange": return { kind, lo: 0, hi: 0.5, lo_feather: 0.1, hi_feather: 0.1 };
      default: return { kind };
    }
  }
  function addShape(kind) {
    const how = maskMenuFor;
    maskMenuFor = null;
    if (how === "new" || !curMask()) {
      st.masks = st.masks || [];
      const id = st.masks.reduce((a, m) => Math.max(a, m.id), 0) + 1;
      st.masks.push({ id, name: SHAPE_NAME[kind], visible: true, invert: false,
                      components: [{ op: "add", invert: false, shape: newShape(kind) }], adjust: {} });
      selMask = st.masks.length - 1; selComp = 0;
    } else {
      curMask().components.push({ op: how === "sub" ? "subtract" : "add", invert: false, shape: newShape(kind) });
      selComp = curMask().components.length - 1;
    }
    overlayOn = true;
    commit();
    renderMaskPanel();
    requestRender(false);
  }

  function maskHtml() {
    return '<div class="lc-head"><h3>Máscaras</h3><button class="lc-btn sm primary" data-a="masknew">+ Nueva</button></div>' +
      '<div class="lc-mlist"></div>' +
      '<div class="lc-mmenu"></div>' +
      '<div class="lc-msel"></div>';
  }
  // Repinta la lista de capas y el detalle de la seleccionada
  function renderMaskPanel() {
    if (!panelBuilt) return;
    const masks = st.masks || [];
    if (selMask >= masks.length) selMask = masks.length - 1;
    const list = el.querySelector(".lc-mlist");
    list.innerHTML = masks.length ? masks.map((m, i) =>
      '<div class="lc-mitem' + (i === selMask ? " on" : "") + '" data-mi="' + i + '">' +
        '<button class="lc-meye' + (m.visible ? "" : " off") + '" data-a="maskeye" title="Mostrar u ocultar">' + (m.visible ? "◉" : "○") + '</button>' +
        '<span class="lc-mname">' + escH(m.name) + '</span>' +
        '<span class="lc-mcount">' + m.components.length + '</span>' +
        '<button class="lc-mdel" data-a="maskdel" title="Borrar máscara">✕</button></div>').join("")
      : '<div class="lc-hint" style="margin:0 12px">Las máscaras aplican ajustes solo a una parte de la foto. Pulsa <b>+ Nueva</b> y elige qué seleccionar.</div>';
    const menu = el.querySelector(".lc-mmenu");
    menu.classList.toggle("on", !!maskMenuFor);
    menu.innerHTML = maskMenuFor ?
      '<div class="lc-sub" style="margin:0 0 6px">' + (maskMenuFor === "new" ? "Nueva máscara" : maskMenuFor === "sub" ? "Restar a la máscara" : "Añadir a la máscara") +
        '<button class="lc-btn sm" data-a="menuclose">Cancelar</button></div>' +
      '<div class="lc-mgrid">' + SHAPES.map(s => '<button class="lc-mshape" data-shape="' + s[0] + '"><b>' + s[1] + '</b><small>' + s[2] + '</small></button>').join("") + '</div>' : "";
    const sel = el.querySelector(".lc-msel");
    const m = curMask();
    if (!m) { sel.innerHTML = ""; layoutMaskLayer(); refreshChrome(); return; }
    const c = curComp();
    let params = "";
    if (c) {
      const k = c.shape.kind;
      if (k === "radial") params = slider("cmp.feather") + '<label class="lc-chk"><input type="checkbox" data-a="cmpinv"' + (c.shape.invert ? " checked" : "") + '> Invertir esta forma</label>';
      else if (k === "luminanceRange") params = many(["cmp.lo", "cmp.hi", "cmp.loFeather", "cmp.hiFeather"]);
      else if (k === "colorRange") params = '<div class="lc-hint">Toca en la foto el color a seleccionar (hasta 5). Colores: <b>' + c.shape.samples.length + '</b> ' +
        (c.shape.samples.length ? '<button class="lc-btn sm" data-a="cmpclear">Quitar colores</button>' : "") + '</div>' + slider("cmp.refine");
      else if (k === "brush") params = '<div class="lc-tabs"><button class="lc-btn sm' + (brush.erase ? "" : " on") + '" data-a="brushpaint">Pintar</button>' +
        '<button class="lc-btn sm' + (brush.erase ? " on" : "") + '" data-a="brusherase">Borrar</button></div>' +
        many(["brush.size", "brush.feather", "brush.flow"]) + '<div class="lc-hint">Pinta sobre la foto con el dedo o el ratón.</div>';
      else if (k === "linear" || k === "radial") params = "";
      else params = '<div class="lc-hint">Selección automática por el motor (heurística, sin IA).</div>';
      if (k === "linear") params = '<div class="lc-hint">Arrastra los puntos, o traza otro degradado arrastrando sobre la foto.</div>';
      if (k === "radial") params = '<div class="lc-hint">Arrastra el centro o los bordes, o traza otro arrastrando sobre la foto.</div>' + params;
    }
    sel.innerHTML =
      '<div class="lc-msec">' +
        '<div class="lc-sub" style="margin-top:4px">Componentes</div>' +
        '<div class="lc-comps">' + m.components.map((cc, i) =>
          '<button class="lc-comp' + (i === selComp ? " on" : "") + '" data-ci="' + i + '">' + (cc.op === "subtract" ? "− " : cc.op === "intersect" ? "∩ " : "+ ") +
          SHAPE_NAME[cc.shape.kind] + '</button>').join("") + '</div>' +
        '<div class="lc-row"><button class="lc-btn sm" data-a="maskadd">+ Añadir</button><button class="lc-btn sm" data-a="masksub">− Restar</button>' +
          (m.components.length > 1 ? '<button class="lc-btn sm" data-a="compdel">Quitar componente</button>' : "") + '</div>' +
        params +
        '<div class="lc-row" style="margin-top:8px">' +
          '<label class="lc-chk"><input type="checkbox" data-a="maskinv"' + (m.invert ? " checked" : "") + '> Invertir máscara</label>' +
          '<label class="lc-chk"><input type="checkbox" data-a="maskov"' + (overlayOn ? " checked" : "") + '> Ver superposición</label></div>' +
        '<div class="lc-row"><button class="lc-btn sm" data-a="maskrename">Renombrar</button><button class="lc-btn sm" data-a="maskreset">Ajustes a cero</button></div>' +
      '</div>' +
      '<div class="lc-sub">Ajustes de la máscara</div>' +
      LOCAL.map(l => slider("mask." + l[0], l[1])).join("");
    el.querySelectorAll(".lc-msel .lc-sl, .lc-mmenu .lc-sl").forEach(syncSlider);
    layoutMaskLayer();
    refreshChrome();
  }
  function maskGet(id) {
    const m = curMask(), c = curComp();
    if (id.startsWith("mask.")) { const k = id.slice(5); return m && k in m.adjust ? m.adjust[k] : specs[id].default; }
    if (id.startsWith("brush.")) return brush[id.slice(6)];
    if (!c) return specs[id].default;
    const s = c.shape;
    switch (id) {
      case "cmp.feather": return s.feather;
      case "cmp.lo": return Math.round(s.lo * 100); case "cmp.hi": return Math.round(s.hi * 100);
      case "cmp.loFeather": return Math.round(s.lo_feather * 100); case "cmp.hiFeather": return Math.round(s.hi_feather * 100);
      case "cmp.refine": return s.refine;
    }
    return 0;
  }
  function maskSet(id, v) {
    const m = curMask(), c = curComp();
    if (id.startsWith("mask.")) {
      if (!m) return;
      const k = id.slice(5);
      if (Math.abs(v - specs[id].default) < 1e-9) delete m.adjust[k]; else m.adjust[k] = v;
      overlayOn = false;   // al ajustar se quiere ver el efecto, no el rojo
      const ov = el.querySelector('[data-a="maskov"]'); if (ov) ov.checked = false;
      return;
    }
    if (id.startsWith("brush.")) { brush[id.slice(6)] = v; return; }
    if (!c) return;
    const s = c.shape;
    switch (id) {
      case "cmp.feather": s.feather = v; break;
      case "cmp.lo": s.lo = Math.min(v / 100, s.hi); break; case "cmp.hi": s.hi = Math.max(v / 100, s.lo); break;
      case "cmp.loFeather": s.lo_feather = v / 100; break; case "cmp.hiFeather": s.hi_feather = v / 100; break;
      case "cmp.refine": s.refine = v; break;
    }
  }
  function maskAction(a, target) {
    const m = curMask();
    if (a === "masknew") { maskMenuFor = "new"; }
    else if (a === "maskadd" && m) { maskMenuFor = "add"; }
    else if (a === "masksub" && m) { maskMenuFor = "sub"; }
    else if (a === "menuclose") { maskMenuFor = null; }
    else if (a === "maskeye") { const i = +target.closest("[data-mi]").dataset.mi; st.masks[i].visible = !st.masks[i].visible; commit(); requestRender(false); }
    else if (a === "maskdel") {
      const i = +target.closest("[data-mi]").dataset.mi;
      if (!confirm("¿Borrar la máscara «" + st.masks[i].name + "»?")) return true;
      st.masks.splice(i, 1); selMask = Math.min(selMask, st.masks.length - 1); selComp = 0; commit(); requestRender(false);
    }
    else if (a === "compdel" && m && m.components.length > 1) { m.components.splice(selComp, 1); selComp = 0; commit(); requestRender(false); }
    else if (a === "maskinv" && m) { m.invert = target.checked; commit(); requestRender(false); }
    else if (a === "cmpinv" && curComp()) { curComp().shape.invert = target.checked; commit(); requestRender(false); }
    else if (a === "maskov") { overlayOn = target.checked; requestRender(false); }
    else if (a === "maskrename" && m) { const n = prompt("Nombre de la máscara", m.name); if (n && n.trim()) { m.name = n.trim().slice(0, 40); commit(); } }
    else if (a === "maskreset" && m) { m.adjust = {}; commit(); requestRender(false); }
    else if (a === "cmpclear" && curComp()) { curComp().shape.samples = []; commit(); requestRender(false); }
    else if (a === "brushpaint") brush.erase = false;
    else if (a === "brusherase") brush.erase = true;
    else return false;
    renderMaskPanel();
    return true;
  }

  // ── Marco de la foto <-> vista (con recorte y enderezado) ─────────────────
  function viewMap() {
    const cv = el.querySelector(".lc-canvas");
    const [fw, fh] = frameSize();
    const c = st.crop, a = st.angle * Math.PI / 180, cs = Math.cos(a), sn = Math.sin(a);
    const cw = cv.offsetWidth, ch = cv.offsetHeight;
    const L = Math.max(fw, fh), scale = cw / ((c[2] - c[0]) * fw);
    return {
      cw, ch, L, scale,
      // marco normalizado -> px de la vista
      to(p) {
        const dx = p.x * fw - fw / 2, dy = p.y * fh - fh / 2;
        const rx = dx * cs - dy * sn + fw / 2, ry = dx * sn + dy * cs + fh / 2;
        return { x: (rx / fw - c[0]) / (c[2] - c[0]) * cw, y: (ry / fh - c[1]) / (c[3] - c[1]) * ch };
      },
      // px de la vista -> marco normalizado
      from(x, y) {
        const rx = (c[0] + x / cw * (c[2] - c[0])) * fw - fw / 2, ry = (c[1] + y / ch * (c[3] - c[1])) * fh - fh / 2;
        return { x: (rx * cs + ry * sn + fw / 2) / fw, y: (-rx * sn + ry * cs + fh / 2) / fh };
      },
    };
  }
  // Capa SVG con los tiradores de la forma elegida
  function layoutMaskLayer() {
    if (!el) return;
    const layer = el.querySelector(".lc-mlayer"), cv = el.querySelector(".lc-canvas");
    const c = tool === "mask" ? curComp() : null;
    layer.style.display = c ? "block" : "none";
    if (!c) return;
    Object.assign(layer.style, { left: cv.offsetLeft + "px", top: cv.offsetTop + "px", width: cv.offsetWidth + "px", height: cv.offsetHeight + "px" });
    const v = viewMap(), s = c.shape;
    let g = "";
    const dot = (p, h, r) => '<circle class="lc-mh" data-mh="' + h + '" cx="' + p.x + '" cy="' + p.y + '" r="' + (r || 9) + '"/>';
    if (s.kind === "linear") {
      const a = v.to(s.start), b = v.to(s.end), dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
      const nx = -dy / len * 2000, ny = dx / len * 2000;
      const line = (p, cls) => '<line class="' + cls + '" x1="' + (p.x - nx) + '" y1="' + (p.y - ny) + '" x2="' + (p.x + nx) + '" y2="' + (p.y + ny) + '"/>';
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      g = line(a, "lc-ml") + line(b, "lc-ml lc-dash") + line(mid, "lc-ml lc-faint") +
          '<line class="lc-ml lc-faint" x1="' + a.x + '" y1="' + a.y + '" x2="' + b.x + '" y2="' + b.y + '"/>' +
          dot(a, "start") + dot(b, "end") + dot(mid, "move", 7);
    } else if (s.kind === "radial") {
      const cpx = v.to(s.center), rx = s.rx * v.L * v.scale, ry = s.ry * v.L * v.scale, ang = s.angle + st.angle;
      const rot = (dx, dy) => { const t = ang * Math.PI / 180; return { x: cpx.x + dx * Math.cos(t) - dy * Math.sin(t), y: cpx.y + dx * Math.sin(t) + dy * Math.cos(t) }; };
      const inner = Math.max(0.01, 1 - s.feather / 100);
      g = '<ellipse class="lc-ml" cx="' + cpx.x + '" cy="' + cpx.y + '" rx="' + rx + '" ry="' + ry + '" transform="rotate(' + ang + ' ' + cpx.x + ' ' + cpx.y + ')"/>' +
          '<ellipse class="lc-ml lc-dash" cx="' + cpx.x + '" cy="' + cpx.y + '" rx="' + rx * inner + '" ry="' + ry * inner + '" transform="rotate(' + ang + ' ' + cpx.x + ' ' + cpx.y + ')"/>' +
          dot(cpx, "center") + dot(rot(rx, 0), "rx") + dot(rot(0, -ry), "ry");
    } else if (s.kind === "colorRange") {
      g = s.samples.length ? "" : "";
    }
    layer.querySelector("svg").innerHTML = g + '<circle class="lc-bcur" r="0" cx="-99" cy="-99"/>';
  }

  function wireMasks() {
    const layer = el.querySelector(".lc-mlayer");
    let drag = null, painting = null;
    const local = (e) => { const r = layer.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    layer.addEventListener("pointerdown", async (e) => {
      const c = curComp();
      if (!c) return;
      e.preventDefault();
      layer.setPointerCapture(e.pointerId);
      const v = viewMap(), [x, y] = local(e), p = v.from(x, y), s = c.shape;
      const h = e.target.closest("[data-mh]");
      if (s.kind === "brush") {
        const stroke = { points: [{ x: p.x, y: p.y }], size: brush.size / 100 * 0.25, feather: brush.feather, flow: brush.flow,
                         density: 100, erase: brush.erase, auto_mask: false };
        s.strokes.push(stroke);
        painting = stroke;
        requestRender(true);
        return;
      }
      if (s.kind === "colorRange") {
        if (s.samples.length >= 5) return;
        try {
          const r = await call({ type: "sample", st: st, x: p.x, y: p.y });
          if (r.lab.length === 3) { s.samples.push(r.lab); overlayOn = true; commit(); renderMaskPanel(); requestRender(false); }
        } catch (err) { /* fuera de la foto */ }
        return;
      }
      if (h) drag = { h: h.dataset.mh, x, y, s0: JSON.parse(JSON.stringify(s)) };
      else if (s.kind === "linear") { s.start = p; s.end = { x: p.x, y: p.y }; drag = { h: "end", x, y, s0: JSON.parse(JSON.stringify(s)) }; }
      else if (s.kind === "radial") { s.center = p; s.rx = s.ry = 0.01; drag = { h: "draw", x, y, s0: JSON.parse(JSON.stringify(s)) }; }
    });
    layer.addEventListener("pointermove", (e) => {
      const c = curComp();
      if (!c) return;
      const v = viewMap(), [x, y] = local(e), p = v.from(x, y), s = c.shape;
      if (s.kind === "brush") {
        const cur = layer.querySelector(".lc-bcur");
        if (cur) { cur.setAttribute("cx", x); cur.setAttribute("cy", y); cur.setAttribute("r", brush.size / 100 * 0.25 * v.L * v.scale); }
        if (painting) {
          const last = painting.points[painting.points.length - 1];
          if (Math.hypot(p.x - last.x, p.y - last.y) > 0.004) { painting.points.push({ x: +p.x.toFixed(4), y: +p.y.toFixed(4) }); requestRender(true); }
        }
        return;
      }
      if (!drag) return;
      const s0 = drag.s0;
      if (drag.h === "start") s.start = p;
      else if (drag.h === "end") s.end = p;
      else if (drag.h === "move" || drag.h === "center") {
        const p0 = v.from(drag.x, drag.y), dx = p.x - p0.x, dy = p.y - p0.y;
        if (s.kind === "linear") { s.start = { x: s0.start.x + dx, y: s0.start.y + dy }; s.end = { x: s0.end.x + dx, y: s0.end.y + dy }; }
        else s.center = { x: s0.center.x + dx, y: s0.center.y + dy };
      } else if (s.kind === "radial") {
        const cpx = v.to(s.center), d = Math.hypot(x - cpx.x, y - cpx.y) / (v.L * v.scale);
        if (drag.h === "rx") s.rx = Math.max(0.01, d);
        else if (drag.h === "ry") s.ry = Math.max(0.01, d);
        else { s.rx = Math.max(0.01, d); s.ry = Math.max(0.01, d * 0.75); }
      }
      layoutMaskLayer();
      requestRender(true);
    });
    const end = () => {
      if (painting) { painting = null; commit(); requestRender(false); }
      if (drag) { drag = null; commit(); requestRender(false); }
    };
    layer.addEventListener("pointerup", end);
    layer.addEventListener("pointercancel", end);
    layer.addEventListener("pointerleave", () => { const cur = layer.querySelector(".lc-bcur"); if (cur) cur.setAttribute("r", 0); });
  }

  // ── Construccion del panel ─────────────────────────────────────────────────
  function sec(key, title, body, open, ids) {
    return '<div class="lc-sec' + (open ? " open" : "") + '" data-sec="' + key + '"' + (ids ? ' data-ids="' + ids + '"' : "") + '>' +
      '<div class="lc-sec-h"><span class="tri"></span><span>' + title + '</span><span class="dot"></span></div>' +
      '<div class="lc-sec-b">' + body + '</div></div>';
  }
  const sub = (t, extra) => '<div class="lc-sub">' + t + (extra || "") + '</div>';
  const many = (ids) => ids.map(id => slider(id)).join("");

  function build() {
    el = document.createElement("div");
    el.className = "lc";
    el.innerHTML =
      '<div class="lc-top">' +
        '<button class="lc-ib" data-a="close" title="Cerrar (Esc)">' + svg(ICON.close) + '</button>' +
        '<span class="lc-name"></span>' +
        '<button class="lc-ib" data-a="undo" title="Deshacer (Ctrl+Z)">' + svg(ICON.undo) + '</button>' +
        '<button class="lc-ib" data-a="redo" title="Rehacer (Ctrl+Mayús+Z)">' + svg(ICON.redo) + '</button>' +
        '<button class="lc-ib" data-a="before" title="Antes / después (\\)">' + svg(ICON.ba) + '</button>' +
        '<button class="lc-ib lc-hide-m" data-a="reset" title="Restablecer todo">' + svg(ICON.reset) + '</button>' +
        '<button class="lc-btn primary" data-a="save" disabled>Guardar</button>' +
      '</div>' +
      '<div class="lc-body">' +
        '<div class="lc-stage">' +
          '<canvas class="lc-canvas"></canvas>' +
          '<div class="lc-crop"><div class="lc-cbox">' +
            ["nw", "n", "ne", "e", "se", "s", "sw", "w"].map(h => '<div class="lc-ch" data-h="' + h + '"></div>').join("") +
          '</div></div>' +
          '<div class="lc-mlayer"><svg></svg></div>' +
          '<div class="lc-badge">Antes</div>' +
          '<div class="lc-busy"><div class="lc-spin"></div><div class="lc-busy-t">Cargando...</div></div>' +
        '</div>' +
        '<div class="lc-tools">' +
          '<button class="lc-ib on" data-tool="edit" title="Editar">' + svg(ICON.edit) + '</button>' +
          '<button class="lc-ib" data-tool="crop" title="Recortar y enderezar (R)">' + svg(ICON.crop) + '</button>' +
          '<button class="lc-ib" data-tool="mask" title="Máscaras (M)">' + svg(ICON.mask) + '</button>' +
        '</div>' +
        '<div class="lc-panel"></div>' +
      '</div>' +
      '<div class="lc-tabbar">' + GROUPS.map(g => '<button class="lc-tab" data-group="' + g[0] + '">' + svg(ICON[g[0] === "masks" ? "mask" : g[0]]) +
        '<span>' + g[1] + '</span></button>').join("") + '</div>';
    document.body.appendChild(el);
    wire();
  }

  // El contenido del panel sale de la tabla de controles del motor, asi que
  // se pinta cuando el Worker la ha mandado
  function panelHtml() {
    return '<div class="lc-hist"><canvas></canvas><div class="lc-hist-info"><span class="lc-dim"></span><span>LightCraft</span></div></div>' +
          '<div class="lc-only-edit">' +
            '<div class="lc-head lc-g-profile"><h3>Editar</h3>' +
              '<button class="lc-btn sm" data-a="auto" title="Ajuste automático de tono">Auto</button>' +
              '<button class="lc-btn sm" data-a="bw" title="Blanco y negro">B y N</button></div>' +
            '<div class="lc-prof lc-g-profile"><label>Perfil</label><select class="lc-select lc-sel-profile"></select></div>' +
            '<div class="lc-prof-amt lc-g-profile" style="padding:0 12px 8px">' + slider("profile.amount") + '</div>' +
            sec("light", "Luz",
              many(["light.exposure", "light.contrast", "light.highlights", "light.shadows", "light.whites", "light.blacks"]) +
              curveHtml(), true) +
            sec("color", "Color",
              sub("Balance de blancos", '<button class="lc-btn sm" data-a="autowb">Auto</button>') +
              many(["wb.tempRel", "wb.tintRel"]) +
              '<div class="lc-only-color">' + many(["color.vibrance", "color.saturation"]) +
                sub("Mezcla de color") +
                '<div class="lc-dots lc-mix-dots">' + BANDS.map((b, i) => '<button class="lc-dotb" data-band="' + i + '" title="' + b[1] + '" style="background:' + b[2] + '"></button>').join("") + '</div>' +
                '<div class="lc-mix-sliders"></div></div>' +
              '<div class="lc-only-bw">' + sub("Mezcla de blanco y negro") +
                BANDS.map(b => slider("bw." + b[0], b[1])).join("") + '</div>' +
              sub("Gradación de color") +
              '<div class="lc-tabs">' + [["shadows", "Sombras"], ["midtones", "Medios"], ["highlights", "Ilum."], ["global", "Global"]]
                .map(z => '<button class="lc-btn sm" data-zone="' + z[0] + '">' + z[1] + '</button>').join("") + '</div>' +
              '<div class="lc-grade-sliders"></div>' + many(["grading.blending", "grading.balance"]),
              true, BANDS.map(b => "mixer." + b[0] + ".hue,mixer." + b[0] + ".sat,mixer." + b[0] + ".lum").join(",") + "," +
                    ["shadows", "midtones", "highlights", "global"].map(z => "grading." + z + ".hue,grading." + z + ".sat,grading." + z + ".lum").join(",")) +
            sec("effects", "Efectos",
              many(["effects.texture", "effects.clarity", "effects.dehaze"]) +
              sub("Viñeta") + many(["vignette.amount", "vignette.midpoint", "vignette.roundness", "vignette.feather", "vignette.highlights"]) +
              sub("Granulado") + many(["grain.amount", "grain.size", "grain.roughness"]), false) +
            sec("detail", "Detalle",
              many(["detail.sharpenAmount", "detail.sharpenRadius", "detail.sharpenDetail", "detail.sharpenMasking"]) +
              sub("Ruido") + many(["detail.nrLuminance", "detail.nrDetail", "detail.nrContrast", "detail.nrColor", "detail.nrColorDetail", "detail.nrColorSmoothness"]), false) +
            sec("optics", "Óptica",
              many(["optics.distortion", "optics.vignetting", "optics.vignettingMidpoint"]) +
              sub("Aberración cromática") + many(["optics.defringePurple", "optics.defringeGreen", "optics.caRed", "optics.caBlue"]), false) +
            sec("geometry", "Geometría",
              many(["geometry.vertical", "geometry.horizontal", "geometry.rotate", "geometry.aspect", "geometry.scale", "geometry.offsetX", "geometry.offsetY"]), false) +
            sec("calibration", "Calibración",
              many(["calibration.shadowsTint", "calibration.redHue", "calibration.redSat", "calibration.greenHue", "calibration.greenSat", "calibration.blueHue", "calibration.blueSat"]), false) +
            '<div class="lc-hint lc-desk-hint" style="padding:12px">Doble clic en un deslizador o en el título de una sección para restablecerlo. ' +
              '<b>\\</b> antes/después · <b>R</b> recortar · <b>Ctrl+Z</b> deshacer. Clic en la foto: zoom 1:1.</div>' +
          '</div>' +
          '<div class="lc-only-mask">' + maskHtml() + '</div>' +
          '<div class="lc-only-crop">' +
            '<div class="lc-head"><h3>Recortar y enderezar</h3></div>' +
            '<div style="padding:0 12px 12px">' +
              sub("Proporción") +
              '<div class="lc-row">' + [[0, "Libre"], [-1, "Original"], [1, "1:1"], [0.8, "4:5"], [1.3333, "4:3"], [1.5, "3:2"], [1.7778, "16:9"]]
                .map(a => '<button class="lc-btn sm" data-aspect="' + a[0] + '">' + a[1] + '</button>').join("") +
                '<button class="lc-btn sm" data-a="swap" title="Vertical / horizontal">⇄</button></div>' +
              sub("Enderezar") + slider("crop.angle") +
              sub("Girar y voltear") +
              '<div class="lc-row">' +
                '<button class="lc-btn sm" data-a="ccw">' + svg(ICON.ccw).replace("<svg", '<svg width="16" height="16" fill="currentColor"') + ' Izq.</button>' +
                '<button class="lc-btn sm" data-a="cw">' + svg(ICON.cw).replace("<svg", '<svg width="16" height="16" fill="currentColor"') + ' Der.</button>' +
                '<button class="lc-btn sm" data-a="fliph">Voltear ↔</button>' +
                '<button class="lc-btn sm" data-a="flipv">Voltear ↕</button></div>' +
              '<div class="lc-row" style="margin-top:12px"><button class="lc-btn sm" data-a="cropreset">Restablecer recorte</button>' +
                '<button class="lc-btn sm primary" data-tool="edit">Hecho</button></div>' +
              '<div class="lc-hint">Arrastra el recuadro o sus tiradores. Girar o voltear sin más cambios se guarda sin pérdida de calidad.</div>' +
            '</div>' +
          '</div>';
  }
  function buildPanel() {
    const panel = el.querySelector(".lc-panel");
    panel.innerHTML = panelHtml();
    panel.querySelector(".lc-sel-profile").innerHTML =
      Object.keys(PROFILES).map(id => '<option value="' + id + '">' + PROFILES[id] + '</option>').join("");
    panelBuilt = true;
    wireCurve();
  }

  // ── Eventos ────────────────────────────────────────────────────────────────
  function wire() {
    const panel = el.querySelector(".lc-panel");
    // Deslizadores (delegado: los de mezcla/gradacion se regeneran)
    panel.addEventListener("input", (e) => {
      const n = e.target.closest(".lc-sl");
      if (!n || !e.target.classList.contains("lc-range")) return;
      onValue(n.dataset.id, parseFloat(e.target.value), true);
      syncSlider(n);
    });
    panel.addEventListener("change", (e) => {
      if (e.target.type === "checkbox" && e.target.dataset.a) { maskAction(e.target.dataset.a, e.target); return; }
      if (e.target.classList.contains("lc-sel-profile")) {
        st.profile = e.target.value;
        if (st.profile === "lc.color") delete st.values["profile.amount"];
        commit();
        requestRender(false);
        return;
      }
      const n = e.target.closest(".lc-sl");
      if (!n) return;
      if (e.target.classList.contains("lc-range")) {
        onValue(n.dataset.id, parseFloat(e.target.value), false);
      } else if (e.target.classList.contains("lc-val")) {
        const v = parseFloat(String(e.target.value).replace(",", ".").replace("+", ""));
        if (!isNaN(v)) onValue(n.dataset.id, v, false);
        e.target.blur();
      }
      syncSlider(n);
      commit();
    });
    panel.addEventListener("keydown", (e) => {
      if (e.target.classList.contains("lc-val") && e.key === "Enter") e.target.blur();
    });
    panel.addEventListener("dblclick", (e) => {
      const n = e.target.closest(".lc-sl");
      if (n && !e.target.classList.contains("lc-val")) {
        onValue(n.dataset.id, specs[n.dataset.id].default, false);
        syncSlider(n);
        commit();
        return;
      }
      const h = e.target.closest(".lc-sec-h");
      if (h) {
        const s = h.parentElement;
        const ids = Array.from(s.querySelectorAll(".lc-sl")).map(x => x.dataset.id).concat((s.dataset.ids || "").split(",").filter(Boolean));
        ids.forEach(id => delete st.values[id]);
        if (s.dataset.sec === "light") { st.curves = { master: [], red: [], green: [], blue: [] }; drawCurve(); }
        syncControls();
        rebindDynamic();
        commit();
        requestRender(false);
      }
    });
    panel.addEventListener("click", (e) => {
      const h = e.target.closest(".lc-sec-h");
      if (h) { h.parentElement.classList.toggle("open"); drawCurve(); return; }
      const band = e.target.closest("[data-band]");
      if (band) { mixBand = +band.dataset.band; rebindDynamic(); return; }
      const zone = e.target.closest("[data-zone]");
      if (zone) { gradeZone = zone.dataset.zone; rebindDynamic(); return; }
      const asp = e.target.closest("[data-aspect]");
      if (asp) { setAspect(+asp.dataset.aspect); return; }
      const shp = e.target.closest("[data-shape]");
      if (shp) { addShape(shp.dataset.shape); return; }
      const ci = e.target.closest("[data-ci]");
      if (ci) { selComp = +ci.dataset.ci; renderMaskPanel(); requestRender(false); return; }
      const mi = e.target.closest("[data-mi]");
      if (mi && !e.target.closest("[data-a]")) { selMask = +mi.dataset.mi; selComp = 0; overlayOn = true; renderMaskPanel(); requestRender(false); }
    });

    el.addEventListener("click", (e) => {
      const t = e.target.closest("[data-tool]");
      if (t) { setTool(t.dataset.tool); return; }
      const g = e.target.closest(".lc-tab[data-group]");
      if (g) { setGroup(g.dataset.group); return; }
      const b = e.target.closest("[data-a]");
      if (!b || b.disabled) return;
      const a = b.dataset.a;
      if (/^(mask|menu|comp|cmp|brush)/.test(a) && b.tagName !== "INPUT") { maskAction(a, b); return; }
      if (a === "close") close(false);
      else if (a === "undo") doUndo();
      else if (a === "redo") doRedo();
      else if (a === "before") toggleBefore();
      else if (a === "reset") { st = freshState(); syncControls(); rebindDynamic(); commit(); requestRender(false); }
      else if (a === "save") save();
      else if (a === "bw") { st.bw = !st.bw; commit(); requestRender(false); }
      else if (a === "auto") runAuto();
      else if (a === "autowb") runAutoWb();
      else if (a === "curvereset") { st.curves[curveCh] = []; drawCurve(); commit(); requestRender(false); }
      else if (a in OPS) orient(a);
      else if (a === "swap") swapAspect();
      else if (a === "cropreset") { st.angle = 0; st.aspect = 0; st.crop = [0, 0, 1, 1]; syncControls(); commit(); requestRender(false); }
    });

    // Clic en la foto: zoom 1:1 (lo que da la vista previa de trabajo)
    el.querySelector(".lc-canvas").addEventListener("click", () => {
      if (tool === "crop") return;
      zoom = !zoom;
      el.querySelector(".lc-stage").classList.toggle("zoom", zoom);
      requestRender(false);
    });
    let rz = null;
    window.addEventListener("resize", () => {
      if (!el.classList.contains("on")) return;
      clearTimeout(rz);
      rz = setTimeout(() => requestRender(false), 150);
    });
    wireCrop();
    wireMasks();
    wireTouch();
  }

  function onValue(id, v, dragging) {
    if (id === "crop.angle") {
      st.angle = Math.max(-45, Math.min(45, v));
      fitCrop();
      if (tool === "crop") { applyTilt(); layoutCrop(); return; }
    } else {
      setVal(id, v);
    }
    requestRender(dragging);
  }

  function keys(e) {
    if (!el.classList.contains("on")) return;
    const typing = e.target && (e.target.tagName === "INPUT" && e.target.type === "text" || e.target.tagName === "SELECT");
    const mod = e.ctrlKey || e.metaKey;
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); if (tool !== "edit") setTool("edit"); else close(false); }
    else if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); if (e.shiftKey) doRedo(); else doUndo(); }
    else if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); doRedo(); }
    else if (typing) return;
    else if (e.key === "\\") { e.preventDefault(); toggleBefore(); }
    else if (e.key === "r" || e.key === "R") { e.preventDefault(); setTool(tool === "crop" ? "edit" : "crop"); }
    else if (e.key === "m" || e.key === "M") { e.preventDefault(); setTool(tool === "mask" ? "edit" : "mask"); }
    else return;
    e.stopPropagation();
  }

  function toggleBefore() {
    before = !before;
    el.querySelector('[data-a="before"]').classList.toggle("on", before);
    requestRender(false);
  }

  function setTool(t) {
    tool = t;
    el.classList.toggle("tool-crop", t === "crop");
    el.classList.toggle("tool-mask", t === "mask");
    if (t === "mask") { maskMenuFor = (st.masks || []).length ? null : "new"; renderMaskPanel(); }
    el.querySelectorAll(".lc-tools [data-tool]").forEach(b => b.classList.toggle("on", b.dataset.tool === t));
    if (t === "crop" && zoom) { zoom = false; el.querySelector(".lc-stage").classList.remove("zoom"); }
    el.querySelector(".lc-stage").classList.toggle("crop", t === "crop");
    el.querySelector(".lc-stage").classList.toggle("masking", t === "mask");
    layoutMaskLayer();
    el.dataset.group = t === "crop" ? "crop" : t === "mask" ? "masks" : (group === "crop" || group === "masks" ? "light" : group);
    if (el.dataset.group !== group) group = el.dataset.group;
    el.querySelectorAll(".lc-tab").forEach(b => b.classList.toggle("on", b.dataset.group === group));
    requestRender(false);
  }
  // Grupo de herramientas (barra inferior del movil)
  let group = "light";
  function setGroup(g) {
    if (g === "auto") { runAuto(); g = "light"; }
    group = g;
    setTool(g === "crop" ? "crop" : g === "masks" ? "mask" : "edit");
    const panel = el.querySelector(".lc-panel");
    if (panel) panel.scrollTop = 0;
    setTimeout(drawCurve, 0);
  }

  async function runAuto() {
    try {
      const r = await call({ type: "auto", st: st });
      for (const [id, v] of Object.entries(r.values)) {
        if (id === "curve.master") st.curves.master = pairs(v);
        else setVal(id, v);
      }
      syncControls();
      drawCurve();
      commit();
      requestRender(false);
    } catch (e) { showMsg("Auto no disponible: " + e.message); }
  }
  async function runAutoWb() {
    try {
      const r = await call({ type: "autoWb", st: st });
      setVal("wb.tempRel", Math.round(r.temp));
      setVal("wb.tintRel", Math.round(r.tint));
      syncControls();
      commit();
      requestRender(false);
    } catch (e) { showMsg("Balance automático no disponible: " + e.message); }
  }

  // ── Recorte ────────────────────────────────────────────────────────────────
  // Mismo modelo que LightCraft: rectangulo normalizado en el marco de la foto
  // (ya girada/volteada) rotado `angle` grados sobre su centro; tiene que caber
  // dentro de la foto girada.
  function frameSize() {
    const swap = st.orient >= 5;
    return swap ? [H, W] : [W, H];
  }
  function maxScale(w, h, ang, cw, ch) {
    const a = Math.abs(ang) * Math.PI / 180, s = Math.sin(a), c = Math.cos(a);
    return Math.min((w / 2) / (cw / 2 * c + ch / 2 * s), (h / 2) / (cw / 2 * s + ch / 2 * c));
  }
  function aspectValue() {
    const [fw, fh] = frameSize();
    return st.aspect === -1 ? fw / fh : st.aspect;
  }
  // El mayor recorte de la proporcion elegida que cabe con el angulo actual
  function fitCrop() {
    const [fw, fh] = frameSize();
    const ar = aspectValue() || (st.angle === 0 ? 0 : fw / fh);
    let bw = fw, bh = fh;
    if (ar) { if (fw / fh > ar) bw = fh * ar; else bh = fw / ar; }
    if (!ar && st.angle === 0) { st.crop = [0, 0, 1, 1]; return; }
    const s = Math.min(1, maxScale(fw, fh, st.angle, bw, bh));
    const cw = bw * s / fw, ch = bh * s / fh;
    st.crop = [0.5 - cw / 2, 0.5 - ch / 2, 0.5 + cw / 2, 0.5 + ch / 2];
  }
  function inside(c) {
    const [fw, fh] = frameSize();
    const a = -st.angle * Math.PI / 180, s = Math.sin(a), co = Math.cos(a);
    const pts = [[c[0], c[1]], [c[2], c[1]], [c[0], c[3]], [c[2], c[3]]];
    return pts.every(([x, y]) => {
      const dx = (x - 0.5) * fw, dy = (y - 0.5) * fh;
      const qx = dx * co - dy * s + fw / 2, qy = dx * s + dy * co + fh / 2;
      return qx >= -0.5 && qy >= -0.5 && qx <= fw + 0.5 && qy <= fh + 0.5;
    });
  }
  function setAspect(a) {
    // Como Lightroom: la proporcion sigue la orientacion de la foto (3:2 en
    // una vertical es 2:3); el boton de intercambio la gira
    const [fw, fh] = frameSize();
    if (a > 0 && a !== 1 && (fw < fh) === (a > 1)) a = 1 / a;
    st.aspect = a;
    fitCrop();
    syncControls();
    commit();
    layoutCrop();
  }
  function swapAspect() {
    const ar = aspectValue();
    if (!ar) return;
    st.aspect = 1 / ar;
    fitCrop();
    syncControls();
    commit();
    layoutCrop();
  }
  function orient(op) {
    st.orient = OPS[op][st.orient];
    if (st.aspect > 0 && (op === "cw" || op === "ccw")) st.aspect = 1 / st.aspect;
    fitCrop();
    commit();
    requestRender(false);
  }

  // Con la herramienta de recorte el motor ensena la foto SIN enderezar: se
  // gira en pantalla (como Lightroom: la foto gira, el recuadro queda recto).
  // Es el mismo modelo que el del motor, asi que no hace falta revelar al
  // mover el angulo.
  function applyTilt() {
    const cv = el.querySelector(".lc-canvas");
    cv.style.transform = tool === "crop" && st.angle ? "rotate(" + st.angle + "deg)" : "";
  }
  function layoutCrop() {
    const cv = el.querySelector(".lc-canvas");
    const layer = el.querySelector(".lc-crop");
    // Caja de maquetacion del lienzo (sin el giro de applyTilt)
    Object.assign(layer.style, { left: cv.offsetLeft + "px", top: cv.offsetTop + "px",
                                 width: cv.offsetWidth + "px", height: cv.offsetHeight + "px" });
    const c = st.crop, box = el.querySelector(".lc-cbox");
    Object.assign(box.style, { left: c[0] * 100 + "%", top: c[1] * 100 + "%",
                               width: (c[2] - c[0]) * 100 + "%", height: (c[3] - c[1]) * 100 + "%" });
  }

  function wireCrop() {
    const layer = el.querySelector(".lc-crop");
    let drag = null;
    layer.addEventListener("pointerdown", (e) => {
      const h = e.target.closest(".lc-ch");
      if (!h && !e.target.closest(".lc-cbox")) return;
      e.preventDefault();
      layer.setPointerCapture(e.pointerId);
      const r = layer.getBoundingClientRect();
      drag = { h: h ? h.dataset.h : "move", x: e.clientX, y: e.clientY, c: st.crop.slice(), w: r.width, h2: r.height };
    });
    layer.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = (e.clientX - drag.x) / drag.w, dy = (e.clientY - drag.y) / drag.h2;
      let [x0, y0, x1, y1] = drag.c;
      const min = 0.04;
      if (drag.h === "move") {
        const w = x1 - x0, hh = y1 - y0;
        x0 = Math.min(1 - w, Math.max(0, x0 + dx)); y0 = Math.min(1 - hh, Math.max(0, y0 + dy));
        x1 = x0 + w; y1 = y0 + hh;
      } else {
        if (drag.h.includes("w")) x0 = Math.min(x1 - min, Math.max(0, x0 + dx));
        if (drag.h.includes("e")) x1 = Math.max(x0 + min, Math.min(1, x1 + dx));
        if (drag.h.includes("n")) y0 = Math.min(y1 - min, Math.max(0, y0 + dy));
        if (drag.h.includes("s")) y1 = Math.max(y0 + min, Math.min(1, y1 + dy));
        const ar = aspectValue();
        if (ar) {
          // Proporcion fija: manda el ancho (o el alto en los tiradores n/s)
          const [fw, fh] = frameSize();
          const wpx = (x1 - x0) * fw, hpx = (y1 - y0) * fh;
          if (drag.h === "n" || drag.h === "s") {
            const nw = hpx * ar / fw, cx = (x0 + x1) / 2;
            x0 = cx - nw / 2; x1 = cx + nw / 2;
          } else {
            const nh = wpx / ar / fh;
            if (drag.h.includes("n")) y0 = y1 - nh; else if (drag.h.includes("s") || drag.h === "e" || drag.h === "w") {
              if (drag.h === "e" || drag.h === "w") { const cy = (y0 + y1) / 2; y0 = cy - nh / 2; y1 = cy + nh / 2; }
              else y1 = y0 + nh;
            }
          }
        }
      }
      const c = [x0, y0, x1, y1];
      if (c.every(v => v >= -1e-6 && v <= 1 + 1e-6) && inside(c)) {
        st.crop = c;
        layoutCrop();
      }
    });
    const end = () => { if (drag) { drag = null; commit(); } };
    layer.addEventListener("pointerup", end);
    layer.addEventListener("pointercancel", end);
  }

  // ── Abrir, cerrar, guardar ─────────────────────────────────────────────────
  function busy(text) {
    const b = el.querySelector(".lc-busy");
    b.classList.toggle("on", !!text);
    if (text) b.querySelector(".lc-busy-t").textContent = text;
  }
  function showMsg(t) {
    busy(t);
    el.querySelector(".lc-busy .lc-spin").style.display = "none";
    setTimeout(() => { el.querySelector(".lc-busy .lc-spin").style.display = ""; busy(null); }, 2600);
  }

  async function open(f) {
    if (!el) build();
    startWorker();
    file = f;
    st = freshState();
    undo = [snap()]; redo = [];
    tool = "edit"; before = false; zoom = false; saving = false;
    mixBand = 0; gradeZone = "shadows"; curveCh = "master"; lastHist = null;
    selMask = -1; selComp = 0; overlayOn = true; maskMenuFor = null; group = "light";
    el.querySelector(".lc-name").textContent = f.name;
    el.classList.remove("tool-crop", "tool-mask");
    el.classList.toggle("m", isMobile());
    el.dataset.group = "light";
    el.querySelectorAll(".lc-tab").forEach(b => b.classList.toggle("on", b.dataset.group === "light"));
    el.querySelector(".lc-stage").classList.remove("zoom", "crop", "masking");
    el.querySelectorAll(".lc-tools [data-tool]").forEach(b => b.classList.toggle("on", b.dataset.tool === "edit"));
    el.querySelector('[data-a="before"]').classList.remove("on");
    const cv = el.querySelector(".lc-canvas");
    cv.width = cv.height = 1;
    el.classList.add("on");
    document.body.classList.add("lc-open");
    document.addEventListener("keydown", keys, true);
    busy("Abriendo con LightCraft...");
    try {
      const r = await call({ type: "open", url: bust("/api/preview?path=" + encodeURIComponent(f.path), f.path) });
      if (file !== f) return;
      W = r.w; H = r.h;
      specs = {};
      r.controls.forEach(sp => { specs[sp.id] = sp; });
      specs["wb.tempRel"] = { id: "wb.tempRel", min: -100, max: 100, default: 0, step: 1, decimals: 0, track: { kind: "temp" } };
      specs["wb.tintRel"] = { id: "wb.tintRel", min: -100, max: 100, default: 0, step: 1, decimals: 0, track: { kind: "tint" } };
      registerMaskSpecs();
      if (!panelBuilt) buildPanel();
      el.querySelector(".lc-dim").textContent = W + " × " + H + " (vista de trabajo)";
      syncControls();
      rebindDynamic();
      renderMaskPanel();
      refreshChrome();
      busy(null);
      requestRender(false);
    } catch (e) {
      busy(null);
      close(true);
      if (window.lcFallback) window.lcFallback(f, e);
    }
  }

  function close(force) {
    if (!el) return;
    if (!force && file && hasChanges() && !confirm("¿Descartar los cambios sin guardar?")) return;
    el.classList.remove("on");
    document.body.classList.remove("lc-open");
    document.removeEventListener("keydown", keys, true);
    file = null;
  }

  async function save() {
    if (!file || saving || !hasChanges()) return;
    saving = true;
    refreshChrome();
    const f = file;
    let data = {};
    try {
      if (!hasPixelEdits()) {
        // Solo giro/volteo: via sin perdida (tag Orientation), como antes
        busy("Guardando...");
        const o = ORIENT_PARAMS[st.orient];
        const res = await fetch("/api/edit", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ path: f.path, rotate: String(o.rotate), flip_h: o.flip_h, flip_v: o.flip_v })
        });
        data = await res.json();
      } else {
        const r = await exportPhoto(f);
        if (!r) { busy(null); saving = false; refreshChrome(); return; }
        busy("Guardando en el NAS...");
        const isPng = r.bytes[0] === 0x89;
        const fd = new FormData();
        fd.append("path", f.path);
        fd.append("file", new Blob([r.bytes], { type: isPng ? "image/png" : "image/jpeg" }), isPng ? "edit.png" : "edit.jpg");
        const res = await fetch("/api/edit_upload", { method: "POST", body: fd });
        data = await res.json();
      }
    } catch (e) {
      data = { error: e.message || "Error de conexión" };
    }
    busy(null);
    saving = false;
    if (data.error) {
      refreshChrome();
      alert("No se pudo guardar: " + data.error);
      return;
    }
    close(true);
    if (window.lcAfterSave) window.lcAfterSave(f.path, data);
  }

  // Revelado a resolucion completa. Un iPhone puede no tener memoria para una
  // foto grande (el motor trabaja en coma flotante: 24 Mpx son ~300 MB): si
  // falla, se rearranca el motor y se ofrece guardar a menor tamano.
  async function exportPhoto(f) {
    const url = "/api/edit_source?path=" + encodeURIComponent(f.path);
    for (const side of [0, 4096, 2560]) {
      busy(side ? "Revelando a " + side + " px..." : "Revelando a resolución completa con LightCraft...\nPuede tardar unos segundos.");
      try {
        return await call({ type: "export", url, st, quality: 95, maxSide: side });
      } catch (e) {
        if (/HTTP|existe|RAW/.test(e.message)) throw e;      // fallo del servidor, no de memoria
        busy("Reiniciando el motor...");
        await restartWorker(f);
        const next = side === 0 ? 4096 : side === 4096 ? 2560 : 0;
        if (!next) throw e;
        busy(null);
        if (!confirm("Este dispositivo no tiene memoria para revelar la foto a " +
                     (side ? side + " px" : "resolución completa") + ".\n¿Guardarla a " + next + " px por el lado mayor?")) return null;
      }
    }
    return null;
  }
  async function restartWorker(f) {
    try { worker.terminate(); } catch (e) { /* ya estaba parado */ }
    worker = null;
    startWorker();
    await call({ type: "open", url: bust("/api/preview?path=" + encodeURIComponent(f.path), f.path) });
  }

  // ── Tactil: iPhone / iPad ─────────────────────────────────────────────────
  // - Deslizadores relativos: se arrastra desde cualquier punto de la fila
  //   (no hay que atinar con el tirador), el desplazamiento vertical sigue
  //   moviendo el panel, y doble toque devuelve el valor por defecto.
  // - Mantener pulsada la foto: ver el "antes" (como Lightroom movil).
  // - Doble toque en la foto: zoom 1:1 / encajar.
  const isMobile = () => window.matchMedia("(max-width: 760px)").matches;
  const coarse = () => window.matchMedia("(pointer: coarse)").matches;
  function wireTouch() {
    const panel = el.querySelector(".lc-panel");
    let t = null, lastTap = { id: null, at: 0 };
    panel.addEventListener("pointerdown", (e) => {
      if (e.pointerType !== "touch") return;
      const n = e.target.closest(".lc-sl");
      if (!n || e.target.classList.contains("lc-val")) return;
      const sp = specs[n.dataset.id];
      if (!sp) return;
      t = { n, sp, id: e.pointerId, x: e.clientX, y: e.clientY, v0: getVal(n.dataset.id), on: false,
            w: n.querySelector(".lc-range").getBoundingClientRect().width || 200 };
    });
    panel.addEventListener("pointermove", (e) => {
      if (!t || e.pointerId !== t.id) return;
      const dx = e.clientX - t.x, dy = e.clientY - t.y;
      if (!t.on) {
        if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) { t = null; return; }   // es scroll
        if (Math.abs(dx) < 6) return;
        t.on = true;
        try { t.n.setPointerCapture(e.pointerId); } catch (err) { /* sin captura */ }
        t.n.classList.add("drag");
      }
      e.preventDefault();
      const span = t.sp.max - t.sp.min;
      let v = t.v0 + dx / t.w * span;
      v = Math.round(v / t.sp.step) * t.sp.step;
      onValue(t.n.dataset.id, Math.min(t.sp.max, Math.max(t.sp.min, v)), true);
      syncSlider(t.n);
    }, { passive: false });
    const up = (e) => {
      if (!t || e.pointerId !== t.id) return;
      const n = t.n, id = n.dataset.id;
      if (t.on) {
        n.classList.remove("drag");
        onValue(id, getVal(id), false);
        commit();
      } else {
        // toque sin arrastrar: doble toque = valor por defecto
        const now = Date.now();
        if (lastTap.id === id && now - lastTap.at < 350) {
          onValue(id, specs[id].default, false); syncSlider(n); commit(); lastTap = { id: null, at: 0 };
        } else lastTap = { id, at: now };
      }
      t = null;
    };
    panel.addEventListener("pointerup", up);
    panel.addEventListener("pointercancel", (e) => { if (t && t.on) { t.n.classList.remove("drag"); commit(); } t = null; });

    // Foto: mantener = antes; doble toque = zoom
    const stage = el.querySelector(".lc-stage");
    let hold = null, held = false, lastStageTap = 0, sx = 0, sy = 0;
    stage.addEventListener("pointerdown", (e) => {
      if (tool !== "edit" || !e.target.closest(".lc-canvas")) return;
      sx = e.clientX; sy = e.clientY; held = false;
      hold = setTimeout(() => { held = true; before = true; el.querySelector(".lc-badge").classList.add("on"); requestRender(false); }, 350);
    });
    stage.addEventListener("pointermove", (e) => {
      if (hold && Math.hypot(e.clientX - sx, e.clientY - sy) > 10) { clearTimeout(hold); hold = null; }
    });
    const release = () => {
      clearTimeout(hold); hold = null;
      if (held) { before = el.querySelector('[data-a="before"]').classList.contains("on"); requestRender(false); }
    };
    stage.addEventListener("pointerup", release);
    stage.addEventListener("pointercancel", release);
    el.querySelector(".lc-canvas").addEventListener("click", (e) => {
      if (held) { held = false; e.stopImmediatePropagation(); return; }
      if (coarse()) {
        // en tactil, un toque no hace nada; el doble toque cambia el zoom
        const now = Date.now();
        if (now - lastStageTap > 350) { lastStageTap = now; e.stopImmediatePropagation(); return; }
        lastStageTap = 0;
      }
    }, true);
  }

  // ¿Puede correr aqui? Hace falta WebAssembly y Workers de modulo
  function supported() {
    return typeof WebAssembly === "object" && typeof Worker === "function";
  }

  window.LightCraftEditor = { open: open, close: close, supported: supported,
                              isOpen: () => !!(el && el.classList.contains("on")),
                              state: () => JSON.parse(JSON.stringify(st)) };
})();
