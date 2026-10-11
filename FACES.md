# Formato `geotagger-faces` (version 1)

Fichero de intercambio de rostros entre el equipo Linux (que hace el
reconocimiento) y Geotagger (que lo muestra). Es un unico JSON, en UTF-8,
normalmente comprimido con gzip (`faces.json.gz`). Geotagger acepta las dos
formas: detecta el gzip por su cabecera, no por la extension.

## Flujo

```
           NAS (/share/.../Fotos)                    Equipo Linux (/mnt/nas/Fotos)
  ┌──────────────────────────────────────┐        ┌───────────────────────────────┐
  │ fotos                                │◀──────▶│ lee las fotos por la red      │
  │ .geotagger/faces.json.gz  (estado) ──┼───────▶│ 1. lee el estado anterior     │
  │                                      │        │ 2. analiza solo nuevas/cambiadas
  │ Geotagger ◀── Ajustes > Actualizar ──┼────────│ 3. escribe faces.json.gz      │
  │   informacion de rostros (subida)    │        │    en el disco del Linux      │
  └──────────────────────────────────────┘        └───────────────────────────────┘
```

- Tras cada importacion Geotagger reescribe `.geotagger/faces.json.gz` dentro de
  la carpeta de trabajo con el estado **fusionado** (`mode: "full"`): incluye
  los nombres puestos en Geotagger y las rutas actualizadas si se movieron
  fotos. Es la fuente de verdad para la siguiente pasada del Linux.
- La carpeta `.geotagger` empieza por punto: la galeria y el indice la ignoran.
- El Linux **no** escribe en `.geotagger/`: su salida va al disco local y el
  usuario la sube desde el navegador.

## Estructura

```json
{
  "format": "geotagger-faces",
  "version": 1,
  "mode": "incremental",
  "generated_at": "2026-10-11T10:00:00Z",
  "generator": {"name": "geotagger-faces-linux", "version": "0.1", "model": "buffalo_l"},
  "root": "Multimedia/Fotos",
  "embedding": {"dim": 512, "dtype": "float32", "metric": "cosine"},
  "persons": [
    {"id": "p_0001", "name": "Ana", "cover": "3f2a9c1e0b7d4a55"},
    {"id": "p_0002", "name": null}
  ],
  "photos": [
    {
      "path": "2024/Viaje/IMG_0001.jpg",
      "size": 4831220,
      "mtime": 1717171717,
      "width": 4000,
      "height": 3000,
      "analyzed_at": "2026-10-11T09:58:12Z",
      "faces": [
        {
          "id": "3f2a9c1e0b7d4a55",
          "box": [0.412, 0.188, 0.091, 0.122],
          "score": 0.98,
          "person": "p_0001",
          "embedding": "<base64 de dim valores dtype, little-endian>"
        }
      ]
    },
    {"path": "2024/Viaje/IMG_0002.jpg", "size": 3911002, "mtime": 1717171720,
     "width": 4000, "height": 3000, "faces": []}
  ]
}
```

### Campos

| Campo | Obligatorio | Descripcion |
|---|---|---|
| `format` | si | Siempre `"geotagger-faces"`. |
| `version` | si | `1`. Geotagger rechaza versiones mayores que la que conoce. |
| `mode` | no | `"incremental"` (por defecto): solo se tocan las fotos que vienen en el fichero. `"full"`: el fichero describe TODA la carpeta de trabajo; las fotos analizadas que no aparezcan se borran del indice de rostros, y tambien las personas sin caras (salvo las que tienen nombre puesto en Geotagger). |
| `generated_at` | no | ISO 8601 UTC. |
| `generator` | no | Libre. `model` se guarda para saber con que se calcularon los embeddings. |
| `root` | no | Informativo: carpeta de trabajo de referencia. |
| `embedding.dim` / `dtype` / `metric` | no | Si se indica `dim`, cada embedding debe medir exactamente `dim * bytes(dtype)`; si no, se descarta ese embedding (la cara se guarda igual). `dtype`: `float32` o `float16`. |
| `persons[].id` | si | Id estable de la persona (cluster). **No debe cambiar entre pasadas**: es lo que une los nombres puestos en Geotagger. |
| `persons[].name` | no | Nombre. Si en Geotagger se le puso nombre a mano, ese manda y la importacion no lo pisa. |
| `persons[].cover` | no | Id de la cara que mejor la representa. |
| `photos[].path` | si | Relativa a la carpeta de trabajo, separador `/`, sin `/` inicial. Debe existir en el NAS; si no, se cuenta como "no encontrada" y se ignora. |
| `photos[].size` / `mtime` | si | Tamano en bytes y fecha de modificacion (segundos, entero) del archivo **en el momento del analisis**. Es lo que permite saber si la foto cambio despues. Se admite ±2 s de diferencia de mtime (redondeo de SMB/NFS). |
| `photos[].width` / `height` | no | Tamano de la imagen tal como se ve (con la orientacion EXIF ya aplicada). |
| `photos[].faces` | si | Lista (vacia si no hay caras: asi consta que la foto ya se analizo). Al importar, las caras de esa foto se **sustituyen** por estas. |
| `faces[].id` | recomendado | Unico en todo el fichero (p. ej. `sha1(path + box)[:16]`). |
| `faces[].box` | si | `[x, y, w, h]` normalizados 0..1 sobre la imagen **ya orientada** (EXIF Orientation aplicada), origen arriba a la izquierda. |
| `faces[].score` | no | Confianza de la deteccion 0..1. |
| `faces[].person` | no | Id de persona, o `null` si no se ha agrupado. |
| `faces[].embedding` | no | Vector en base64. Se guarda en Geotagger y vuelve al Linux en la copia de `.geotagger/` para agrupar caras nuevas sin recalcular las viejas. |

## Que hace Geotagger con el fichero

- `POST /api/faces/import` (multipart, campo `file`): fusiona y reescribe
  `.geotagger/faces.json.gz`. Responde con un resumen (`photos`, `faces`,
  `persons`, `missing`, `invalid`, `removed`).
- `GET /api/faces/status`: fotos analizadas, caras, personas, fotos pendientes
  (nuevas o cambiadas desde el ultimo analisis) y datos de la ultima importacion.
- `GET /api/faces/export`: descarga el estado fusionado.
- `GET /api/faces/photo?path=...`: caras de una foto (con `stale: true` si la
  foto cambio despues de analizarla).
- `GET /api/faces/persons` y `POST /api/faces/person` (`{"id", "name"}`):
  listar personas y ponerles nombre.

Mover, copiar, renombrar o borrar fotos desde Geotagger actualiza las rutas de
sus caras (una copia recibe ids de cara nuevos y la misma persona).
