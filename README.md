# GeoTagger

Aplicación web (Flask) para geoetiquetar, fechar, renombrar y organizar las
fotos guardadas en el NAS. Toda la escritura de metadatos se hace con
**ExifTool** (`-overwrite_original`), que reescribe únicamente los segmentos de
metadatos y copia los píxeles byte a byte: las fotos nunca se recomprimen.

## Carpeta de trabajo

La app trabaja sobre dos niveles:

| Concepto | Qué es | Cómo se fija |
|---|---|---|
| **Raíz del NAS** (`NAS_ROOT`) | Todo lo que el contenedor puede recorrer. Es el límite: no se puede salir de ahí. | Variable de entorno `NAS_ROOT`. Si no se indica, se usa el primer directorio que exista de `/nas`, `/share` o `/photos`. |
| **Carpeta de trabajo** | La raíz de la galería: la carpeta que se ve al abrir la app. | Desde la interfaz, botón **Cambiar** del panel de carpetas. Se guarda en `settings.json`. También se puede fijar la inicial con `WORK_ROOT` (relativa a `NAS_ROOT`). |

Para poder navegar por más carpetas del NAS hay que **montarlas en el
contenedor**: el selector solo ve lo que está montado. Por ejemplo, montando
todos los recursos compartidos del NAS:

```sh
docker run -d --name geotagger \
  -p 5000:5000 \
  -v /share:/nas \
  -v /ruta/a/datos:/app/data \
  -e NAS_ROOT=/nas \
  geotagger
```

Con esa configuración, el botón **Cambiar** permite recorrer todo `/share` y
elegir cualquier carpeta como grupo de trabajo. Sin `NAS_ROOT`, y con solo
`/photos` montado, la app se comporta como antes: la galería se limita a esa
carpeta.

## Otras variables de entorno

| Variable | Para qué sirve |
|---|---|
| `SMTP_USER` / `SMTP_PASS` | Cuenta desde la que se envían los reportes por email (el destinatario se configura en Ajustes). |

## Requisitos

- `exiftool` instalado en la imagen (lectura y escritura de metadatos).
- Dependencias de Python en `requirements.txt`.
