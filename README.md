# prodrafts2pdf

Convierte un archivo de **Pro Drafts** (`.prodrafts` o `.prodrafts.zip`) en un **PDF** donde los trazos del Apple Pencil son **vectores** (zoom ilimitado sin pérdida de calidad), conservando el PDF de fondo de cada página, las imágenes insertadas, las formas y las líneas.

Funciona en **Linux, macOS y Windows** (solo Python).

## Qué exporta

- Trazos de PencilKit (pluma, rotulador, lápiz) como curvas vectoriales, con su color y grosor.
- Borrados y movimientos: los trazos borrados con la goma o con garabato no aparecen, los recortados solo muestran los tramos visibles, y los movidos salen en su posición final.
- PDF de fondo de cada página (tamaño original). Si el fondo es oscuro, se invierten los neutros (blanco → negro) para que se lea sobre papel blanco.
- Imágenes insertadas (PNG, JPEG, HEIC) con su posición, rotación y escala.
- Formas (rectángulos, resaltados) y líneas, incluidas las **discontinuas**.

## Instalación

Requiere Python 3.8 o superior.

```bash
git clone <url-del-repo>
cd prodrafts2pdf

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Imágenes HEIC (fotos del iPhone/iPad)

Las fotos insertadas desde Apple suelen ser `.heic`. El script las abre con la primera opción disponible:

1. `pillow-heif` (ya incluido en `requirements.txt`).
2. `sips` (macOS).
3. `heif-convert` (Linux: `sudo apt install libheif-examples`).
4. ImageMagick (`magick` / `convert`) o `ffmpeg`.

Si ninguna funciona, esa imagen se omite y se muestra un aviso; el resto del PDF se genera igual.

## Uso

```bash
python3 prodrafts2pdf.py nota.prodrafts.zip salida.pdf
```

El primer argumento puede ser el `.zip`, el paquete `.prodrafts` ya descomprimido (una carpeta) o la carpeta que lo contenga.

Opciones:

| Opción | Descripción | Por defecto |
|---|---|---|
| `--width F` | Factor de grosor de los trazos | `0.55` |
| `--canvas-scale F` | Escala lienzo → página PDF | `0.5` |
| `--hide-masked` | Oculta por completo cualquier trazo con registro de edición (en vez de quitar solo los tramos borrados) | desactivado |

Ejemplo:

```bash
python3 prodrafts2pdf.py nota.prodrafts.zip salida.pdf --width 0.6
```

## Cómo funciona

Un `.prodrafts` es un paquete con esta estructura:

```
pageIndex.plist           lista de páginas (uuid, PDF de fondo, índice)
drawings/<uuid>.com.apple.drawing   trazos de la página (PencilKit)
attachments/              PDFs de fondo e imágenes insertadas
note.database.sqlite      elementos extra (imágenes, formas, líneas, tablas...)
```

El script hace lo siguiente para cada página:

1. **Lee `pageIndex.plist`** para saber el orden de páginas y qué PDF de fondo usa cada una.
2. **Decodifica el `.drawing`** (un protobuf de PencilKit) con un parser mínimo incluido en el propio script: tintas, trazos, puntos (x, y, grosor) y transformaciones afines. Interpreta también los registros de edición (campo 11) para aplicar borrados, recortes y movimientos.
3. **Lee `ZDRAFTBOARDITEM`** de la base SQLite para obtener imágenes, formas y líneas (se ignoran los elementos marcados como borrados).
4. **Dibuja** con ReportLab, en una capa vectorial a escala `--canvas-scale` desde la esquina superior izquierda: primero imágenes y formas, encima los trazos.
5. **Combina** esa capa con la página original del PDF de fondo (PyPDF) y escribe el PDF final.

## Limitaciones

- No se renderiza el contenido de tablas ni de cuadros de texto del tablero; solo trazos, imágenes, formas y líneas.
- Los ajustes de grosor (`--width`) están calibrados contra la miniatura de un archivo concreto; puede que otros cuadernos requieran un pequeño retoque.
- El formato de Pro Drafts no es público: se ha deducido por ingeniería inversa y podría cambiar.

## Dependencias

`reportlab`, `pypdf`, `pillow` y, opcionalmente, `pillow-heif`.
