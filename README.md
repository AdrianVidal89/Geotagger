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

## Organizar

- **Nueva carpeta**: desde la barra superior se crea una carpeta dentro de la
  que estes viendo, y tambien desde el propio selector de destino.
- **Mover o copiar**: con fotos seleccionadas, el selector de destino recorre la
  carpeta de trabajo (nunca puede salirse de ella) y permite crear la carpeta
  destino sobre la marcha. Mover es una operacion de sistema de archivos; copiar
  duplica el archivo byte a byte conservando sus fechas. En ninguno de los dos
  casos se abre ni se recodifica la imagen. Si en el destino ya hay un archivo
  con el mismo nombre, se anade un sufijo en vez de sobrescribirlo.

## Indice de metadatos

Leer el GPS de cada foto con ExifTool es proporcional al numero de archivos, y
un RAW tarda bastante en abrirse: con miles de fotos, abrir el mapa se hacia
esperar. Ahora hay un indice en SQLite (`/app/data/index.db`, dentro del volumen
de datos) que se mantiene en segundo plano:

- Cada foto se lee UNA vez. Solo se vuelve a leer si cambia su fecha o su tamano.
- El repaso corre en un hilo aparte y por lotes, asi que el mapa se abre al
  instante con lo que ya se sabe y se va completando solo mientras se mira.
- Se repasa al arrancar, cada 15 minutos, al cambiar la carpeta de trabajo y al
  subir fotos. Nunca dos repasos a la vez, y no se repite si acaba de hacerse.
- Las operaciones de la app actualizan el indice sin releer nada: al escribir GPS
  ya se conocen las coordenadas, mover o renombrar solo cambia la ruta de la
  fila, copiar la duplica y borrar la quita.
- El indice es una cache: si se borra el fichero, se reconstruye solo.

Ademas del mapa, el filtro **Sin GPS** se sirve del indice. Antes lanzaba un
proceso de ExifTool POR FOTO; con 3.000 fotos de prueba pasa de minutos a 0,09 s.

## Favoritos

La estrella del panel de carpetas marca la carpeta que se esta viendo. Los
favoritos se guardan como rutas completas dentro del NAS, asi que pulsarlos
cambia la carpeta de trabajo a esa ruta y la galeria arranca alli, vengas de
donde vengas.

## Mapa

Pulsando el logotipo se abren en un mapa las fotos de la carpeta actual y sus
subcarpetas que tengan coordenadas, servidas por el indice (ver arriba): la
primera apertura tarda lo que tarde el indice en construirse, y a partir de ahi
es instantanea. El mapa no usa ninguna libreria externa: los mosaicos de OpenStreetMap
se colocan segun la proyeccion Mercator y los marcadores se agrupan por celdas de
pantalla, de modo que mil fotos del mismo sitio no tapan el mapa. Pulsando un
grupo se acerca; pulsando una foto se ve su miniatura y se puede abrir en el
visor. Las imagenes del mapa las descarga el navegador, asi que el dispositivo
necesita internet; si no lo tiene, avisa pero las fotos siguen situadas y se
pueden abrir.

## Edicion de fotos

Desde el visor (boton de ajustes en la cabecera) se pueden girar, recortar y
ajustar luz y color las fotos:

- **Girar y voltear** es SIN PERDIDA: solo se reescribe el tag EXIF
  `Orientation`, los pixeles no se tocan. Funciona incluso en RAW. Si la unica
  edicion es un giro, se guarda por esta via automaticamente.
- **Recortar y ajustar** obligan a recodificar los pixeles: se sobrescribe el
  original a calidad 95, avisando antes. Los metadatos (GPS, fechas, camara) se
  copian con ExifTool al archivo resultante.
- **Herramientas de restauracion** (pensadas para fotos antiguas):
  - *Equilibrar color*: estira cada canal RGB con su propio rango. Quita de
    golpe la dominante amarillenta del papel viejo y devuelve el contraste, algo
    que un estirado comun no puede hacer (aplasta el canal que vive en la franja
    mas baja).
  - *Punto negro* y *punto blanco*: niveles manuales para una foto desvaida.
  - *Sombras* y *luces*: recuperan detalle en las zonas oscuras o quemadas.
  - *Temperatura* y *tinte*: afinan a mano la dominante (ambar-azul y
    verde-magenta).
  - *Auto restaurar*: calcula el equilibrio por canal y el relleno de sombras, y
    lo deja puesto en los controles para poder retocarlo. Tambien se puede
    aplicar a VARIAS fotos a la vez desde el boton "Auto restaurar" del panel:
    cada foto se analiza por separado.
  - *Enderezar*: rota +-15 grados y recorta al mayor rectangulo centrado con la
    misma proporcion, de modo que no quedan esquinas vacias. El factor depende
    solo de la proporcion y del angulo, asi que el editor calcula exactamente el
    mismo encuadre que el servidor.
  - *Nitidez*: una sola mascara de enfoque cubre las dos direcciones. Hacia la
    derecha realza el detalle (escaneados, fotos blandas); hacia la izquierda
    suaviza el grano. El radio del desenfoque va con la resolucion, asi que la
    vista previa muestra el mismo efecto relativo que el archivo final.
- **Los RAW no se pueden sobrescribir**: al recortar o ajustar uno se guarda un
  JPEG nuevo junto al original (`nombre_edit.jpg`) y el RAW se queda intacto.

### Coste de guardar

Escribir metadatos con ExifTool obliga a reescribir el archivo entero, y en PNG
sale caro: casi 3 s en uno de 23 MB (unas 3 veces mas por MB que en JPEG),
mientras que LEERLOS cuesta centesimas. Por eso al guardar:

- El bloque EXIF del original (fechas, GPS, camara) se incrusta al escribir el
  archivo, con Orientation puesto a 1 porque el giro ya va en los pixeles.
- Solo se pasa por ExifTool cuando de verdad hace falta: si el original lleva
  XMP o IPTC, que no caben en el EXIF, o si no se pudo leer su EXIF (un RAW).
  Un PNG sin metadatos no necesita ninguna pasada.
- En PNG no se usa `optimize`: prueba varias estrategias para ahorrar un 2% de
  tamano y con archivos grandes no compensa.
- La miniatura y la vista previa del archivo editado se dejan hechas en la
  cache, porque el navegador las pide justo despues y regenerarlas obliga a
  decodificar otra vez la foto entera.

Con un PNG de 23 MB, un recorte pasa de 3,9 s a 1,6 s, y la vista previa
posterior de 0,22 s a 0,01 s.

La vista previa del navegador y el resultado guardado usan la misma cadena, en
el mismo orden (giro y volteo -> enderezado -> recorte -> nitidez -> equilibrio
por canal -> niveles -> temperatura y tinte -> sombras y luces -> exposicion ->
contraste -> saturacion). Los ajustes tonales se calculan con un LUT por canal
en ambos lados, y el desenfoque de la nitidez replica ImageFilter.BoxBlur de
Pillow (bordes repetidos, redondeo hacia arriba y cuantizacion entre pasadas),
comprobado bit a bit contra su salida. Lo que se ve es lo que se guarda; la
unica diferencia son unas pocas unidades en bordes muy marcados, porque la
vista previa parte de una version recomprimida de la foto.

El boton Auto no aplica nada por su cuenta: deja los valores en los controles.

La nitidez se aplica ANTES de los ajustes tonales. Ademas de ser el orden
habitual (enfocar el escaneado y graduarlo despues), permite al editor guardar
su resultado: mover exposicion, contraste o color no repite el desenfoque, y
los controles siguen respondiendo igual de rapido con la nitidez activada.
Mientras se arrastra un control que si obliga a recalcular (enderezar o la
propia nitidez) la vista previa baja de resolucion, y al soltar se vuelve a la
resolucion completa.

## Otras variables de entorno

| Variable | Para qué sirve |
|---|---|
| `SMTP_USER` / `SMTP_PASS` | Cuenta desde la que se envían los reportes por email (el destinatario se configura en Ajustes). |

## Construir y arrancar

El repositorio incluye `Dockerfile` y `docker-compose.yml` listos para el NAS:

```sh
docker compose up -d --build
```

O a mano, si prefieres no usar compose:

```sh
docker build -t geotagger .
docker run -d --name geotagger --restart unless-stopped \
  -p 5000:5000 \
  -v /share:/share \
  -v geotagger_geotagger_settings:/app/data \
  -e NAS_ROOT=/share \
  geotagger
```

Los volumenes no se pueden cambiar en un contenedor ya creado: para anadir o
cambiar un montaje hay que recrearlo (`docker rm -f geotagger` y volver a
lanzarlo). Los datos y las fotos viven en el NAS, no en el contenedor, asi que
recrearlo no pierde nada — pero conviene mantener el mismo montaje de
`/app/data` para conservar los ajustes y la cache de miniaturas.

## Requisitos

- `exiftool` instalado en la imagen (lo instala el `Dockerfile`).
- Dependencias de Python en `requirements.txt`.
- El montaje del NAS debe permitir escritura: si es de solo lectura, la
  galeria se ve pero fallan GPS, fecha y renombrado.
