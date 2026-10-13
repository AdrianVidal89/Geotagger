"""Genera el logotipo de GeoTagger (SVG) en static/icons/.

Un pin de ubicacion (geo) cuya cabeza es el diafragma de una camara (foto).
Las piezas salen de geometria, no de un editor: para retocarlo se cambian
las constantes y se vuelve a ejecutar. Los PNG se renderizan despues con
tools/render_icons.mjs (Chromium de Playwright).

    python3 tools/make_logo.py && node tools/render_icons.mjs
"""
import math, os

OUT = os.path.join(os.path.dirname(__file__), "..", "static", "icons")
S = 512
CX, CY, R = 256, 212, 132          # cabeza del pin
TIP = 452                          # punta
LENS = 92                          # radio del objetivo
HOLE = 30                          # apertura (hexagono central)
BLADES = 6
TWIST = 18                         # giro del diafragma (grados)

def f(v):
    return ("%.2f" % v).rstrip("0").rstrip(".")

def pin_path(cx, cy, r, tip):
    d = tip - cy
    a = math.acos(r / d)                     # angulo entre el radio y la vertical
    t1 = math.pi / 2 + a, math.pi / 2 - a    # tangentes (izq., der.)
    lx, ly = cx + r * math.cos(t1[0]), cy + r * math.sin(t1[0])
    rx, ry = cx + r * math.cos(t1[1]), cy + r * math.sin(t1[1])
    # La punta se redondea un poco: con un pico perfecto se ve agresivo
    k = 0.10
    bx1, by1 = rx + (cx - rx) * (1 - k), ry + (tip - ry) * (1 - k)
    bx2, by2 = lx + (cx - lx) * (1 - k), ly + (tip - ly) * (1 - k)
    return ("M%s %sA%s %s 0 1 1 %s %sL%s %sQ%s %s %s %sZ" %
            (f(lx), f(ly), f(r), f(r), f(rx), f(ry), f(bx1), f(by1),
             f(cx), f(tip + 2), f(bx2), f(by2)))

def iris(cx, cy, lens, hole, n, twist):
    """Laminas del diafragma: cada una va de un lado del hexagono central al
    borde del objetivo, prolongando ese lado (asi se ven las palas girando)."""
    V = [(cx + hole * math.cos(math.radians(twist + 360 * i / n)),
          cy + hole * math.sin(math.radians(twist + 360 * i / n))) for i in range(n)]
    def hit(p, q):
        # prolonga p->q hasta el circulo del objetivo
        dx, dy = q[0] - p[0], q[1] - p[1]
        L = math.hypot(dx, dy); dx /= L; dy /= L
        fx, fy = p[0] - cx, p[1] - cy
        b = fx * dx + fy * dy
        c = fx * fx + fy * fy - lens * lens
        t = -b + math.sqrt(b * b - c)
        return p[0] + dx * t, p[1] + dy * t
    blades = []
    for i in range(n):
        a, b = V[i], V[(i + 1) % n]
        e1 = hit(a, b)                         # final del lado a->b
        c = V[(i + 2) % n]
        e0 = hit(b, c)                         # final del lado siguiente
        blades.append("M%s %sL%s %sL%s %sA%s %s 0 0 1 %s %sZ" % (
            f(a[0]), f(a[1]), f(b[0]), f(b[1]), f(e1[0]), f(e1[1]),
            f(lens), f(lens), f(e0[0]), f(e0[1])))
    hexa = "M" + "L".join("%s %s" % (f(x), f(y)) for x, y in V) + "Z"
    return blades, hexa

def favicon():
    """Version para 16-48 px: el pin ocupa todo, sin rejilla ni brillos, y el
    diafragma con menos laminas y trazo grueso para que se lea en la pestana."""
    blades, hexa = iris(CX, CY, LENS + 6, HOLE + 10, 6, TWIST)
    p = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">',
         '<defs><linearGradient id="pin" x1="0" y1="0" x2="0" y2="1">'
         '<stop offset="0" stop-color="#5ff4ff"/><stop offset="1" stop-color="#0a7ae0"/></linearGradient></defs>',
         '<rect width="512" height="512" rx="120" fill="#06121c"/>',
         '<g transform="translate(256 262) scale(1.12) translate(-256 -262)">',
         '<path d="%s" fill="url(#pin)"/>' % pin_path(CX, CY, R, TIP),
         '<circle cx="%d" cy="%d" r="%d" fill="#06121c"/>' % (CX, CY, LENS + 14)]
    for d in blades:
        p.append('<path d="%s" fill="#2fe6ff" stroke="#06121c" stroke-width="12" stroke-linejoin="round"/>' % d)
    p.append('<path d="%s" fill="#06121c"/></g></svg>' % hexa)
    return "\n".join(p) + "\n"

def logo(bg="square", scale=1.0):
    """bg: 'square' (icono de app, a sangre), 'rounded' (favicon, esquinas
    transparentes) o None (solo el pin, para la cabecera)."""
    blades, hexa = iris(CX, CY, LENS, HOLE, BLADES, TWIST)
    tones = ["#2fe6ff", "#19c8f0", "#0ea5e0", "#2fe6ff", "#19c8f0", "#0ea5e0"]
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">',
             '<defs>',
             '<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
             '<stop offset="0" stop-color="#0b2233"/><stop offset="1" stop-color="#040a12"/></linearGradient>',
             '<linearGradient id="pin" x1="0" y1="0" x2="0" y2="1">'
             '<stop offset="0" stop-color="#5ff4ff"/><stop offset=".55" stop-color="#14b8e8"/>'
             '<stop offset="1" stop-color="#0a6fd6"/></linearGradient>',
             '<radialGradient id="glow" cx=".5" cy=".42" r=".5">'
             '<stop offset="0" stop-color="#00e5ff" stop-opacity=".28"/>'
             '<stop offset="1" stop-color="#00e5ff" stop-opacity="0"/></radialGradient>',
             '<radialGradient id="lens" cx=".42" cy=".36" r=".7">'
             '<stop offset="0" stop-color="#123a52"/><stop offset="1" stop-color="#050d16"/></radialGradient>',
             '</defs>']
    if bg == "square":
        parts.append('<rect width="512" height="512" fill="url(#bg)"/>')
    elif bg == "rounded":
        parts.append('<rect width="512" height="512" rx="112" fill="url(#bg)"/>')
    if bg:
        # Rejilla de meridianos y paralelos, apenas insinuada: el "geo"
        grid = []
        for x in (96, 176, 336, 416):
            grid.append('<path d="M%d 0V512"/>' % x)
        for y in (120, 300, 390):
            grid.append('<path d="M0 %dH512"/>' % y)
        parts.append('<g stroke="#1d4a63" stroke-width="2" opacity=".55">' + "".join(grid) + '</g>')
        parts.append('<circle cx="256" cy="230" r="230" fill="url(#glow)"/>')
        # Sombra del pin en el suelo
        parts.append('<ellipse cx="256" cy="462" rx="58" ry="11" fill="#00e5ff" opacity=".22"/>')
    g = '<g transform="translate(256 256) scale(%s) translate(-256 -256)">' % f(scale)
    parts.append(g)
    parts.append('<path d="%s" fill="url(#pin)"/>' % pin_path(CX, CY, R, TIP))
    parts.append('<circle cx="%d" cy="%d" r="%d" fill="url(#lens)"/>' % (CX, CY, LENS + 8))
    for d, t in zip(blades, tones):
        parts.append('<path d="%s" fill="%s" stroke="#06121c" stroke-width="3.5" stroke-linejoin="round"/>' % (d, t))
    parts.append('<path d="%s" fill="#040a12"/>' % hexa)
    # Brillo del cristal
    parts.append('<path d="M%s %sA%s %s 0 0 1 %s %s" fill="none" stroke="#ffffff" stroke-opacity=".55" '
                 'stroke-width="7" stroke-linecap="round"/>' % (
                     f(CX - 62), f(CY - 46), f(LENS - 14), f(LENS - 14), f(CX - 20), f(CY - 76)))
    parts.append('</g></svg>')
    return "\n".join(parts) + "\n"

if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    files = {
        "logo.svg": logo("rounded"),             # logotipo completo (README, documentos)
        "favicon.svg": favicon(),                # pestana del navegador y cabecera
        "icon-square.svg": logo("square"),       # iOS / Android (a sangre)
        "icon-maskable.svg": logo("square", .82),  # Android adaptable: zona segura
    }
    for name, svg in files.items():
        with open(os.path.join(OUT, name), "w") as fh:
            fh.write(svg)
        print(name)
