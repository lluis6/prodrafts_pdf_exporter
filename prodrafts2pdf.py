#!/usr/bin/env python3
"""
prodrafts2pdf.py - Convierte un archivo Pro Drafts (.prodrafts / .prodrafts.zip)
en un PDF con los trazos del Apple Pencil como VECTORES (calidad ilimitada al
hacer zoom), conservando el PDF de fondo de cada página.
También extrae el texto que Apple (Vision) reconoció de la escritura a mano.

Uso:
    python3 prodrafts2pdf.py nota.prodrafts.zip salida.pdf [--width 0.55] [--canvas-scale 0.5] [--hide-masked]

Dependencias: pip install reportlab pypdf pillow   (HEIC: pillow-heif, o sips en macOS / heif-convert, ImageMagick o ffmpeg en Linux)
"""
import argparse, glob, io, math, os, plistlib, struct, sys, tempfile, zipfile

# ---------------------------------------------------------------- protobuf mínimo
def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]; i += 1
        r |= (c & 127) << s; s += 7
        if c < 128:
            return r, i

def pb(b):
    """Devuelve [(campo, wiretype, valor)]"""
    i, out = 0, []
    while i < len(b):
        k, i = _varint(b, i); f, t = k >> 3, k & 7
        if t == 0:
            v, i = _varint(b, i)
        elif t == 2:
            l, i = _varint(b, i); v = b[i:i + l]; i += l
        elif t == 1:
            v = b[i:i + 8]; i += 8
        elif t == 5:
            v = b[i:i + 4]; i += 4
        else:
            raise ValueError("wire type %d" % t)
        out.append((f, t, v))
    return out

def pbd(b):
    d = {}
    for f, t, v in pb(b):
        d.setdefault(f, []).append(v)
    return d

# ---------------------------------------------------------------- PencilKit (.drawing)
# Formato: "wrd\xf0" + versión + protobuf.
#   campo 4 (repetido) = tintas  {1: color RGBA floats, 2: nombre 'com.apple.ink.pen'...}
#   campo 5 (repetido) = trazos  {4: índice de tinta, 5: trayectoria, 7: transformación afín}
#   trayectoria: 3 = nº de puntos, 4 = máscara de atributos presentes por punto,
#                6 = valores constantes de los atributos ausentes, 7 = puntos
#   punto: x,y (f32) + [t f32][tamaño f32][3 x u16][u16 ...] según máscara
# Borrados y movimientos:
#   - trazo SIN campo 5 (trayectoria)  -> trazo eliminado (lápida): no se dibuja.
#   - trazo con campos 9 + 11 = registro de edición (movido y/o recortado/borrado con goma o garabato):
#       * 11.7  = transformación afín (a b c d tx ty) de la posición nueva (sustituye a la del campo 7)
#       * 11.12 = parejas (inicio, fin) de la trayectoria que SIGUEN VISIBLES.
#         Sin parejas -> no queda nada (garabato/trazo borrado). Parejas parciales -> solo esos tramos.
#   - campo 13/14 = trazo enderezado/forma (mantener el lápiz al final): NO es un borrado.
# Imágenes insertadas (PNG/JPEG/HEIC) y formas: tabla ZDRAFTBOARDITEM de note.database.sqlite.
BIT_SIZE = {0: 8, 1: 4, 2: 4, 5: 2, 6: 2, 7: 2, 8: 2}

def parse_drawing(path):
    raw = open(path, 'rb').read()
    if raw[:3] != b'wrd':
        raise ValueError("No es un PKDrawing: %s" % path)
    top = pbd(raw[8:])
    inks = []
    for v in top.get(4, []):
        d = pbd(v)
        col = [struct.unpack('<f', x)[0] for f, t, x in pb(d[1][0])] if 1 in d else [0, 0, 0, 1]
        thick = struct.unpack('<d', d[8][0])[0] if 8 in d and len(d[8][0]) == 8 else 0.0
        inks.append({'color': col, 'name': d[2][0].decode('utf8', 'replace') if 2 in d else '',
                     'fixed': 4 in d and d[4][0] == b'fixed-width', 'thick': thick})
    strokes = []
    for v in top.get(5, []):
        m = pbd(v)
        if 5 not in m:           # lápida de un trazo borrado
            continue
        p = pbd(m[5][0])
        if 3 not in p or 7 not in p:
            continue
        n, mask = p[3][0], p[4][0]
        data, desc = p[7][0], (p[6][0] if 6 in p else b'')
        rec = sum(sz for b_, sz in BIT_SIZE.items() if mask >> b_ & 1)
        if rec * n != len(data):
            rec = len(data) // n          # respaldo si hay un bit desconocido
        # valores constantes (orden: t, tamaño, ...)
        off = 0
        if not mask & 2: off += 4
        const_size = None
        if not mask & 4:
            const_size = struct.unpack('<f', desc[off:off + 4])[0]
        pts = []
        for i in range(n):
            r = data[i * rec:(i + 1) * rec]
            x, y = struct.unpack('<ff', r[:8]); o = 8
            t = 0.0
            if mask & 2:
                t, = struct.unpack('<f', r[o:o + 4]); o += 4
            w = const_size
            if mask & 4:
                w, = struct.unpack('<f', r[o:o + 4]); o += 4
            pts.append((x, y, w))
        # pluma de ancho fijo: el grosor lo marca el ajuste de la pluma (0.5, 0.8...), no el 'tamaño'
        # guardado (2 + grosor), que apenas distingue 0.5 de 0.8. 0.8 -> 2.8 (como antes), 0.5 -> 1.75
        ink0 = inks[m[4][0]] if 4 in m and m[4][0] < len(inks) else None
        if ink0 and ink0['fixed'] and ink0['thick'] > 0:
            pts = [(x, y, FIXED_UNIT * ink0['thick']) for x, y, _ in pts]
        # transformación afín propia del trazo (a b c d tx ty)
        a, b, c, d_, tx, ty = 1, 0, 0, 1, 0, 0
        if 7 in m:
            vals = [struct.unpack('<f', x)[0] for f, t, x in pb(m[7][0])]
            if len(vals) == 6:
                a, b, c, d_, tx, ty = vals
        ink = inks[m[4][0]] if 4 in m and m[4][0] < len(inks) else {'color': [0, 0, 0, 1], 'name': ''}

        def place(T):
            # el escalado del trazo (p.ej. un título agrandado x1.43) también engrosa la línea
            a_, b_, c_, d2, tx_, ty_ = T
            k = math.sqrt(abs(a_ * d2 - b_ * c_)) or 1.0
            return [(a_ * x + c_ * y + tx_, b_ * x + d2 * y + ty_, None if w is None else w * k) for x, y, w in pts]

        def entry(p, ranges, masked, moved):
            return {'pts': p, 'ink': ink, 'ts': m[8][0] if 8 in m else 0, 'ranges': ranges, 'masked': masked,
                    'moved': moved, 'has11': 11 in m, 'r11': [x for r in ranges for x in r]}

        if 11 not in m:
            if 10 in m:
                # trazo con máscara propia (campos 6/10/12 en el propio trazo): 12 = tramos visibles.
                # Sin 12 no queda nada visible (son los puntitos sueltos que deja la goma): no se dibuja.
                fl = [struct.unpack('<f', x)[0] for x in m.get(12, [])]
                rg = [(fl[i], fl[i + 1]) for i in range(0, len(fl) - 1, 2)]
                strokes.append(entry(place((a, b, c, d_, tx, ty)), rg, True, False))
            else:
                strokes.append(entry(place((a, b, c, d_, tx, ty)), [], None, False))
            continue
        # Campo 11 (REPETIDO) = fragmentos que siguen visibles tras editar el trazo (goma, lazo, rayas de borrar).
        # Cada fragmento puede anidar su historial (11 > 11.11 > ...; el más externo es el más reciente):
        #   7  = transformación afín ABSOLUTA (trayectoria original -> posición actual) de la última edición que la define
        #   12 = parejas (inicio, fin) de la trayectoria original que ocupa ese fragmento
        found = False
        for raw in m[11]:
            chain, cur = [pbd(raw)], pbd(raw)
            while 11 in cur:
                cur = pbd(cur[11][0]); chain.append(cur)
            T, moved, ranges = (a, b, c, d_, tx, ty), False, []
            for q in chain:
                if 7 in q:
                    mv = [struct.unpack('<f', x)[0] for f, t, x in pb(q[7][0])]
                    if len(mv) == 6:
                        T, moved = tuple(mv), True
                        break
            for q in chain:
                fl = [struct.unpack('<f', x)[0] for x in q.get(12, [])]
                if fl:
                    ranges = [(fl[i], fl[i + 1]) for i in range(0, len(fl) - 1, 2)]
                    break
            if ranges:
                found = True
                strokes.append(entry(place(T), ranges, True, moved))
        if not found:      # ningún fragmento con rangos: era un garabato / trazo borrado
            strokes.append(entry(place((a, b, c, d_, tx, ty)), [], True, False))
    return strokes

def apply_mask(st, hide=False):
    """Trazos con campo 11 (registro de edición; el trazo original lleva el campo 9 de 'sustituido'):
         - 11.12 = parejas (inicio, fin) de la trayectoria que SIGUEN VISIBLES (índices fraccionarios de punto)
         - sin parejas      -> no queda nada visible: era un garabato/trazo borrado -> no se dibuja
         - rango = todo     -> el trazo se dibuja entero
         - rangos parciales -> se dibujan solo esos tramos (el resto lo borró la goma / el garabato de borrado)
       Si además hay 11.7 el trazo se movió: parse_drawing ya aplicó esa transformación.
       hide=True (--hide-masked): oculta todo trazo con campo 11."""
    if not st.get('masked'):
        return [st]
    rg = sorted(st.get('ranges') or [])
    if hide or not rg:
        return []
    pts = st['pts']; n = len(pts)
    if n == 1:
        return [st]
    def lerp(a, b, t):
        w = None if (a[2] is None or b[2] is None) else a[2] + (b[2] - a[2]) * t
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, w)
    def at(t):
        t = max(0.0, min(t, n - 1.0)); i = min(int(t), n - 2); return lerp(pts[i], pts[i + 1], t - i)
    out = []
    for lo, hi in rg:
        lo, hi = max(lo, 0.0), min(hi, n - 1.0)
        if hi - lo < 1e-3:
            continue
        seg = [at(lo)] + [pts[i] for i in range(n) if lo < i < hi] + [at(hi)]
        partial = lo > 1e-3 or hi < n - 1.0 - 1e-3
        if partial and sum(math.hypot(seg[i + 1][0] - seg[i][0], seg[i + 1][1] - seg[i][1])
                           for i in range(len(seg) - 1)) < MIN_FRAGMENT:
            continue          # restos minúsculos que deja la goma / el garabato: puntitos sueltos
        out.append(dict(st, pts=seg))
    return out

# ---------------------------------------------------------------- geometría
def _ccw(poly):
    a = sum(poly[i][0] * poly[(i + 1) % len(poly)][1] - poly[(i + 1) % len(poly)][0] * poly[i][1]
            for i in range(len(poly)))
    return poly if a >= 0 else poly[::-1]

def _disc(cx, cy, r, k=14):
    return [(cx + r * math.cos(2 * math.pi * i / k), cy + r * math.sin(2 * math.pi * i / k)) for i in range(k)]

def dedupe(pts, eps=0.05):
    out = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - out[-1][0]) > eps or abs(p[1] - out[-1][1]) > eps:
            out.append(p)
    return out

WIDTH_SCALE = 0.55   # calibrado contra la miniatura del propio archivo
FIXED_UNIT = 3.5     # unidades de lienzo por cada 1.0 de grosor de pluma fija (0.8 -> 2.8)
CANVAS_SCALE = 0.5   # lienzo del bloc -> página PDF (medido: el lienzo mide el doble que el A4)
HIDE_MASKED = False  # False: movidos (11.7) se dibujan en su sitio y los borrados se quitan según sus rangos; --hide-masked: oculta todo trazo con campo 11
MIN_FRAGMENT = 4.0   # unidades de lienzo: los tramos visibles de un trazo recortado más cortos que esto se descartan
DARK_PAPER = False   # si el fondo es oscuro: blanco->negro (texto) y fondo->blanco

def map_rgb(r, g, b):
    if DARK_PAPER:
        if max(r, g, b) - min(r, g, b) < 0.3:     # neutros (blanco, gris, negro) se invierten
            return 1 - r, 1 - g, 1 - b
    return r, g, b

def draw_stroke(c, st, H, ox, oy, eraser_color):
    from reportlab.lib.colors import Color
    ink = st['ink']; name = ink['name']
    pts = dedupe(st['pts']) if len(st['pts']) > 1 else st['pts']
    r, g, b, a = ink['color'][:4]
    if 'eraser' in name:
        r, g, b, a = eraser_color + (1.0,)
    else:
        r, g, b = map_rgb(r, g, b)
    if 'marker' in name:
        a = min(a, 0.40)
    elif 'pencil' in name:
        a = min(a, 0.85)
    c.setFillColor(Color(r, g, b, alpha=a)); c.setStrokeColor(Color(r, g, b, alpha=a))
    X = lambda x: x - ox
    Y = lambda y: H - (y - oy)
    ws = [(p[2] or 1.0) * WIDTH_SCALE for p in pts]
    if len(pts) == 1:
        c.circle(X(pts[0][0]), Y(pts[0][1]), ws[0] / 2, stroke=0, fill=1); return
    if max(ws) - min(ws) <= 0.04 * max(ws):          # grosor constante → trazo nativo
        c.setLineWidth(sum(ws) / len(ws)); c.setLineCap(1); c.setLineJoin(1)
        path = c.beginPath(); path.moveTo(X(pts[0][0]), Y(pts[0][1]))
        if len(pts) == 2:
            path.lineTo(X(pts[1][0]), Y(pts[1][1]))
        else:
            for i in range(1, len(pts) - 1):          # curvas por puntos medios
                mx, my = (pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2
                if i == 1:
                    sx, sy = pts[0][0], pts[0][1]
                p0 = (sx, sy) if i == 1 else prev
                q = pts[i]; e = (mx, my)
                # cuadrática → cúbica
                c1 = (p0[0] + 2 / 3 * (q[0] - p0[0]), p0[1] + 2 / 3 * (q[1] - p0[1]))
                c2 = (e[0] + 2 / 3 * (q[0] - e[0]), e[1] + 2 / 3 * (q[1] - e[1]))
                path.curveTo(X(c1[0]), Y(c1[1]), X(c2[0]), Y(c2[1]), X(e[0]), Y(e[1]))
                prev = e
            path.lineTo(X(pts[-1][0]), Y(pts[-1][1]))
        c.drawPath(path, stroke=1, fill=0)
        return
    # grosor variable → unión de cuadriláteros + discos (relleno no-cero)
    path = c.beginPath()
    def add(poly):
        poly = _ccw(poly)
        path.moveTo(X(poly[0][0]), Y(poly[0][1]))
        for q in poly[1:]:
            path.lineTo(X(q[0]), Y(q[1]))
        path.close()
    for i in range(len(pts) - 1):
        (x1, y1, _), (x2, y2, _) = pts[i], pts[i + 1]
        dx, dy = x2 - x1, y2 - y1; l = math.hypot(dx, dy) or 1
        nx, ny = -dy / l, dx / l; w1, w2 = ws[i] / 2, ws[i + 1] / 2
        add([(x1 + nx * w1, y1 + ny * w1), (x2 + nx * w2, y2 + ny * w2),
             (x2 - nx * w2, y2 - ny * w2), (x1 - nx * w1, y1 - ny * w1)])
    for (x, y, _), w in zip(pts, ws):
        add(_disc(x, y, w / 2))
    # fillMode 1 = nonzero winding
    c.drawPath(path, stroke=0, fill=1, fillMode=1)
    # reportlab usa even-odd por defecto en drawPath(fillMode)
    # (se corrige abajo con fillMode)

# ---------------------------------------------------------------- formas (rectángulos, resaltados...)
def board_items(root, page_uuid):
    """Formas de la tabla ZDRAFTBOARDITEM (p.ej. el recuadro de color translúcido)."""
    import sqlite3, re
    db = os.path.join(root, 'note.database.sqlite')
    out = []
    if not os.path.isfile(db):
        return out
    def ka(b):
        try:
            o = plistlib.loads(b)['$objects']; return o
        except Exception:
            return None
    con = sqlite3.connect(db)
    cols = [r[1] for r in con.execute('pragma table_info(ZDRAFTBOARDITEM)')]
    for row in con.execute('select * from ZDRAFTBOARDITEM'):
        r = dict(zip(cols, row))
        if r.get('ZPAGEID') != page_uuid or r.get('ZISDELETE'):
            continue
        fo = ka(r['ZFRAMEVALUE']) if r.get('ZFRAMEVALUE') else None
        if not fo:
            continue
        nums = [float(x) for x in re.findall(r'-?\d+(?:\.\d+)?(?:e-?\d+)?', next(x for x in fo if isinstance(x, str) and x.startswith('{')))]
        if len(nums) != 4:
            continue
        def col(key):
            o = ka(r[key]) if r.get(key) else None
            d = next((x for x in (o or []) if isinstance(x, dict) and 'UIRed' in x), None)
            return (d['UIRed'], d['UIGreen'], d['UIBlue'], d['UIAlpha']) if d else None
        tr = (1, 0, 0, 1, 0, 0)
        try:
            to = plistlib.loads(r['ZTRANSFORMVALUE'])['$objects']
            dd = next(x for x in to if isinstance(x, dict) and 'NS.atval.a' in x)
            tr = tuple(float(dd['NS.atval.' + k]) for k in ('a', 'b', 'c', 'd', 'tx', 'ty'))
        except Exception:
            pass
        ctr = None
        try:
            co = ka(r['ZCENTER'])
            sx = next(x for x in co if isinstance(x, str) and x.startswith('{'))
            ctr = tuple(float(v) for v in re.findall(r'-?\d+(?:\.\d+)?', sx))[:2]
        except Exception:
            pass
        pts_ = {}
        if r.get('ZTYPE') == 5:          # línea / conector (p.ej. separador discontinuo)
            for key in ('ZSTARTPOINT', 'ZENDPOINT'):
                try:
                    po = ka(r[key])
                    sp = next(x for x in po if isinstance(x, str) and x.startswith('{'))
                    pts_[key] = tuple(float(v) for v in re.findall(r'-?\d+(?:\.\d+)?(?:e-?\d+)?', sp))[:2]
                except Exception:
                    pass
        out.append({'line': pts_ if len(pts_) == 2 else None, 'dash': (r.get('ZLINEDASHWIDTH') or 0, r.get('ZLINEDASHSPACE') or 0) if r.get('ZLINETYPE') else None,
                    'cap': r.get('ZLINECAP'), 'frame': nums, 'image': r.get('ZFILENAME') or None, 'tr': tr, 'center': ctr,
                    'alpha': r['ZALPHA'] if r.get('ZALPHA') is not None else 1.0,
                    'fill': col('ZFILLCOLOR') if r.get('ZFILLCOLORENABLE') else None,
                    'stroke': col('ZSTOKECOLOR') if r.get('ZSTOKECOLORENABLE') else None,
                    'lw': r.get('ZLINEWIDTH') or 0, 'radius': r.get('ZCORNERRADIUS') or 0,
                    'order': r.get('ZZLEVEL') or 0})
    return sorted(out, key=lambda d: d['order'])

_IMG_CACHE = {}

def load_image(path):
    """Abre una imagen adjunta (PNG/JPEG/HEIC...) como PIL RGBA. None si no se puede leer."""
    if path in _IMG_CACHE:
        return _IMG_CACHE[path]
    from PIL import Image
    im = None
    try:
        try:
            im = Image.open(path); im.load()
        except Exception:
            im = None
            if path.lower().endswith(('.heic', '.heif')):
                im = _open_heic(path)
        if im is not None:
            im = im.convert('RGBA')
    except Exception:
        im = None
    _IMG_CACHE[path] = im
    return im

def _open_heic(path):
    import subprocess, shutil
    from PIL import Image
    try:
        import pillow_heif; pillow_heif.register_heif_opener()
        return Image.open(path)
    except Exception:
        pass
    tmp = os.path.join(tempfile.mkdtemp(prefix='heic_'), 'o.png')
    cmds = []
    if shutil.which('sips'):
        cmds.append(['sips', '-s', 'format', 'png', path, '--out', tmp])      # macOS
    if shutil.which('heif-convert'):
        cmds.append(['heif-convert', path, tmp])                              # Linux (libheif-examples)
    if shutil.which('magick'):
        cmds.append(['magick', path, tmp])
    elif shutil.which('convert'):
        cmds.append(['convert', path, tmp])
    if shutil.which('ffmpeg'):
        cmds.append(['ffmpeg', '-loglevel', 'error', '-y', '-i', path, tmp])
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            if os.path.isfile(tmp):
                return Image.open(tmp)
        except Exception:
            continue
    return None

def draw_image_item(c, it, root, H, ox, oy):
    from reportlab.lib.utils import ImageReader
    im = load_image(os.path.join(root, 'attachments', it['image']))
    x, y, w, h = it['frame']
    if im is None:
        print("  aviso: no puedo leer la imagen %s (se omite)" % it['image'], file=sys.stderr)
        return
    a, b, cc, d, _, _ = it['tr']
    cx, cy = it['center'] or (x + w / 2, y + h / 2)
    c.saveState()
    try:
        c.setFillAlpha(it.get('alpha', 1.0))
    except Exception:
        pass
    # centro del elemento y transformación (rotación/escala) alrededor de él; eje y invertido (y hacia arriba = -y)
    c.translate(cx - ox, H - (cy - oy))
    c.transform(a, -b, -cc, d, 0, 0)
    c.drawImage(ImageReader(im), -w / 2, -h / 2, w, h, mask='auto')
    c.restoreState()

def draw_item(c, it, H, ox, oy):
    from reportlab.lib.colors import Color
    x, y, w, h = it['frame']
    X, Y = x - ox, H - (y - oy) - h
    if it.get('line'):                     # línea (continua o discontinua), sin relleno
        if not it['stroke']:
            return
        r, g, b, a = it['stroke']; r, g, b = map_rgb(r, g, b)
        c.saveState(); c.setStrokeColor(Color(r, g, b, alpha=a)); c.setLineWidth(it['lw'])
        c.setLineCap({'round': 1, 'square': 2}.get(it.get('cap'), 0))
        if it.get('dash') and it['dash'][0] and it['dash'][1]:
            c.setDash(it['dash'][0], it['dash'][1])
        (x1, y1), (x2, y2) = it['line']['ZSTARTPOINT'], it['line']['ZENDPOINT']
        c.line(x1 - ox, H - (y1 - oy), x2 - ox, H - (y2 - oy)); c.restoreState(); return
    if it['fill']:
        r, g, b, a = it['fill']; r, g, b = map_rgb(r, g, b); c.setFillColor(Color(r, g, b, alpha=a))
    if it['stroke']:
        r, g, b, a = it['stroke']; r, g, b = map_rgb(r, g, b); c.setStrokeColor(Color(r, g, b, alpha=a)); c.setLineWidth(it['lw'])
    c.roundRect(X, Y, w, h, min(it['radius'], w / 2, h / 2),
                stroke=1 if it['stroke'] and it['lw'] else 0, fill=1 if it['fill'] else 0)

# ---------------------------------------------------------------- paquete
def open_package(src):
    if os.path.isdir(src):
        root = src
    else:
        tmp = tempfile.mkdtemp(prefix='prodrafts_')
        zipfile.ZipFile(src).extractall(tmp)
        root = tmp
    cand = [p for p in glob.glob(os.path.join(root, '**', 'pageIndex.plist'), recursive=True)
            if '__MACOSX' not in p]
    if not cand:
        raise SystemExit("No encuentro pageIndex.plist: ¿es un .prodrafts?")
    return os.path.dirname(cand[0])

def _bg_color_from_pdf(bgpdf, idx):
    """Color del primer relleno del PDF de fondo leyendo su contenido (sin herramientas externas)."""
    import re
    from pypdf import PdfReader
    try:
        data = PdfReader(bgpdf).pages[idx].get_contents().get_data().decode('latin-1')
    except Exception:
        return None
    m = re.search(r'([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+(?:sc|rg)\b', data)
    if m:
        return tuple(float(v) for v in m.groups())
    m = re.search(r'([\d.]+)\s+(?:sc|g)\b', data)
    if m:
        v = float(m.group(1)); return (v, v, v)
    return None

def bg_is_dark(bgpdf, idx, ext_hex):
    """¿Fondo oscuro? 1º color de relleno del PDF; 2º pdftoppm si existe; 3º color de la ficha."""
    import subprocess, shutil
    if os.path.isfile(bgpdf):
        col = _bg_color_from_pdf(bgpdf, idx)
        if col:
            return 0.299 * col[0] + 0.587 * col[1] + 0.114 * col[2] < 0.45
    if shutil.which('pdftoppm') and os.path.isfile(bgpdf):
        try:
            tmp = os.path.join(tempfile.mkdtemp(prefix='pdbg_'), 'bg')
            subprocess.run(['pdftoppm', '-r', '8', '-f', str(idx + 1), '-l', str(idx + 1), '-gray', '-singlefile',
                            bgpdf, tmp], check=True, capture_output=True)
            data = open(tmp + '.pgm', 'rb').read()
            parts = data.split(b'\n', 3)           # P5 / w h / 255 / pixeles
            px = parts[3]
            return sum(px) / len(px) < 110
        except Exception:
            pass
    r, g, b = hex2rgb(ext_hex, (1, 1, 1))
    return 0.299 * r + 0.587 * g + 0.114 * b < 0.45

def hex2rgb(h, default=(0.2, 0.2, 0.2)):
    try:
        h = h.lstrip('#'); return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except Exception:
        return default

def convert(src, out):
    """Devuelve el nº de páginas. Cada página sale con el tamaño original del PDF de fondo."""
    global DARK_PAPER
    from reportlab.pdfgen import canvas as rl
    from pypdf import PdfReader, PdfWriter
    root = open_package(src)
    pages = plistlib.load(open(os.path.join(root, 'pageIndex.plist'), 'rb'))
    writer = PdfWriter()
    for pn, pg in enumerate(pages, 1):
        uid = pg['uuid']
        bgpdf = os.path.join(root, 'attachments', pg.get('sourceName', ''))
        bgpage = PdfReader(bgpdf).pages[pg.get('sourceIndex', 0)] if os.path.isfile(bgpdf) else None
        W0 = float(bgpage.mediabox.width) if bgpage else 595.28
        H0 = float(bgpage.mediabox.height) if bgpage else 841.89
        dfile = os.path.join(root, 'drawings', uid + '.com.apple.drawing')
        strokes = parse_drawing(dfile) if os.path.isfile(dfile) else []
        strokes = [p for s_ in strokes for p in apply_mask(s_, HIDE_MASKED) if len(p['pts']) >= 1]
        items = board_items(root, uid)
        DARK_PAPER = bg_is_dark(bgpdf, pg.get('sourceIndex', 0), pg.get('extensionBackgroundColorHex', '#FFFFFF'))
        xs = [p[0] for s in strokes for p in s['pts']] + [v for it in items for v in (it['frame'][0], it['frame'][0] + it['frame'][2])]
        ys = [p[1] for s in strokes for p in s['pts']] + [v for it in items for v in (it['frame'][1], it['frame'][1] + it['frame'][3])]
        # La página del bloc es el PDF de fondo (tamaño original) y el lienzo se coloca a escala fija
        # desde la esquina superior izquierda: lo que no haya dibujado queda en blanco y lo que
        # caiga fuera de la página no existe en el documento (no se reduce ni se recentra nada).
        sc, tx, ty = CANVAS_SCALE, 0.0, 0.0
        fuera = sum(1 for s_ in strokes if any(p[0] * sc < 0 or p[1] * sc < 0 or p[0] * sc > W0 or p[1] * sc > H0
                                                for p in s_['pts']))
        buf = io.BytesIO(); c = rl.Canvas(buf, pagesize=(W0, H0)); c.setPageCompression(1)
        paper = (1, 1, 1) if DARK_PAPER else None
        if paper:
            c.setFillColorRGB(*paper); c.rect(0, 0, W0, H0, stroke=0, fill=1)
        clip = c.beginPath(); clip.rect(0, 0, W0, H0); c.clipPath(clip, stroke=0, fill=0)
        c.saveState(); c.translate(tx, H0 - ty); c.scale(sc, sc)   # a partir de aquí: coordenadas del dibujo, y hacia arriba = -y
        for it in items:
            if it.get('image'):
                draw_image_item(c, it, root, 0, 0, 0)
            else:
                draw_item(c, it, 0, 0, 0)
        er = (1.0, 1.0, 1.0)
        for s in strokes:
            draw_stroke(c, s, 0, 0, 0, er)
        c.restoreState(); c.showPage(); c.save()
        top = PdfReader(io.BytesIO(buf.getvalue())).pages[0]
        if bgpage is not None and not DARK_PAPER:      # el PDF original va debajo
            base = PdfWriter(); base.add_page(bgpage); b = base.pages[0]; b.merge_page(top); top = b
        writer.add_page(top)
        print("página %d: %d trazos%s, %.0fx%.0f pt%s" % (pn, len(strokes), ", fondo oscuro→blanco" if DARK_PAPER else "", W0, H0,
                                                       ", %d trazos fuera de la página (recortados)" % fuera if fuera else ""))
    writer.write(out)
    return len(pages)

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('src'); ap.add_argument('out')
    ap.add_argument('--width', type=float, default=None, help='factor de grosor (por defecto %.2f)' % WIDTH_SCALE)
    ap.add_argument('--canvas-scale', type=float, default=None, help='escala lienzo->página (por defecto %.2f)' % CANVAS_SCALE)
    ap.add_argument('--hide-masked', action='store_true', help='ocultar por completo los trazos borrados que tengan algún rango (por defecto solo se quitan los tramos borrados)')
    a = ap.parse_args()
    if a.width: WIDTH_SCALE = a.width
    if a.canvas_scale: CANVAS_SCALE = a.canvas_scale
    HIDE_MASKED = a.hide_masked
    convert(a.src, a.out)
