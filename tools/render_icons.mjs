// Renderiza los PNG de los iconos a partir de los SVG de static/icons/
// (los genera tools/make_logo.py). Usa el Chromium de Playwright:
//   node tools/render_icons.mjs [ruta/a/playwright/index.mjs]
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
const pw = process.argv[2] || 'playwright';
const { chromium } = await import(pw);
const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'static', 'icons');
const jobs = [
  ['icon-square.svg', 'apple-touch-icon.png', 180],   // iPad / iPhone: pantalla de inicio
  ['icon-square.svg', 'icon-192.png', 192],           // Android / Chrome
  ['icon-square.svg', 'icon-512.png', 512],
  ['icon-maskable.svg', 'icon-maskable-512.png', 512], // Android adaptable
  ['favicon.svg', 'favicon-16.png', 16],
  ['favicon.svg', 'favicon-32.png', 32],
  ['favicon.svg', 'favicon-48.png', 48],
];
const b = await chromium.launch();
for (const [src, out, size] of jobs) {
  const p = await b.newPage({ viewport: { width: size, height: size } });
  const data = 'data:image/svg+xml;base64,' + fs.readFileSync(path.join(dir, src)).toString('base64');
  await p.setContent(`<html><body style="margin:0;background:transparent"><img src="${data}" width="${size}" height="${size}" style="display:block"></body></html>`);
  await p.waitForFunction(() => document.images[0].complete);
  await p.screenshot({ path: path.join(dir, out), omitBackground: true });
  await p.close();
  console.log(out);
}
await b.close();
