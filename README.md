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

## Rendimiento en carpetas grandes

Con miles de fotos indexandose de fondo, la app se habia vuelto lenta. Lo que
la frenaba no era el repaso en si, sino cuatro cosas que se hacian por foto:

- **Un proceso de ExifTool por foto en cada lote.** Cambiar la fecha o el GPS de
  una seleccion arrancaba un proceso por archivo. Ahora se manda un solo
  comando con todas las fotos del lote (de 200 en 200). Solo si el lote no
  actualiza exactamente los archivos esperados se repite una a una, para poder
  decir cual fallo. Un desplazamiento de fecha NO se reintenta nunca: se
  releen las fechas del lote y se compara, porque repetirlo desplazaria dos
  veces las que si funcionaron.
- **La cache de miniaturas no se limpiaba.** El nombre del fichero era
  `md5(ruta + fecha + formato)` pero al borrar se buscaba por `md5(ruta)`: no
  coincidia nunca, asi que no se borraba nada y la carpeta crecia sin limite;
  encima se recorria entera en cada operacion (21 ms por foto con 8.000
  ficheros). Ahora el nombre empieza por el hash de la RUTA, las miniaturas se
  reparten en 256 subcarpetas y borrar las de una foto mira solo la suya.
- **Se releia con ExifTool lo que el indice ya sabia.** Abrir una foto o
  renombrar lanzaba procesos para leer GPS y fecha. Ahora se sirven del indice
  siempre que la foto no haya cambiado (misma fecha y tamano), y las
  operaciones anotan en el indice lo que acaban de escribir en vez de marcar la
  foto para releerla.
- **Los nombres de lugar se consultaban una y otra vez.** Nominatim obliga a
  esperar un segundo entre peticiones, y se agrupaba por 11 metros: casi una
  consulta por foto. Ahora se agrupa por ~1,1 km (a ese zoom el nombre es el
  del pueblo o barrio) y se guardan en la base para siempre. Si la red falla,
  se deja de intentar en el resto del lote en vez de esperar por cada foto.

Ademas, cada hilo de Flask (uno por peticion) abria su propia conexion SQLite;
ahora se comparte una sola. El repaso de fondo lee con `-fast2` (corta en
cuanto tiene los metadatos, sin leerse el archivo entero) y descansa entre
lotes para no comerse el disco mientras se trabaja.

### PNG grandes

Escribir metadatos en un PNG grande cuesta mucho mas que en un JPEG, y no es
por el disco: copiar el archivo entero tarda 0,02 s, pero ExifTool lo recorre
en Perl a unos 10 MB/s. Un PNG de 18 MB cuesta 1,8 s frente a 0,33 s de un
JPEG de 30 MB, que es MAS grande. Da igual el modo de escritura
(`-overwrite_original`, en sitio o a un archivo nuevo): los tres tardan lo
mismo.

Eso es tiempo de CPU, y el tiempo de CPU si se reparte: un lote grande se
divide entre varios procesos de ExifTool (uno por nucleo, hasta cuatro). Ocho
PNG de 18 MB pasan de 14,8 s a 3,6 s. Solo se reparte cuando el lote pesa mas
de 24 MB; con fotos normales una sola llamada ya va en decimas y arrancar mas
procesos no aporta nada.

Medido sobre una carpeta de 2.700 fotos:

| Operacion | Antes | Ahora |
|---|---|---|
| Abrir una foto (GPS y fecha) | 0,19 s | 0,00 s |
| Cambiar la fecha a 20 fotos | 5,79 s | 0,33 s |
| Poner GPS a 20 fotos | 4,6 s | 0,25 s |
| Renombrar 20 fotos por EXIF | 39 s | 0,01 s |
| Filtro Sin GPS | minutos | 0,01 s |

## Filtros de fecha

Los filtros **Desde** y **Hasta** (y el orden) van por la fecha de los
METADATOS, la fecha real en que se hizo la foto, no por la del archivo. La
diferencia importa: una foto de 2005 copiada al NAS tiene fecha de archivo de
hoy, asi que filtrando por ella "Desde" parecia funcionar (todo es posterior a
cualquier fecha que pongas) mientras que "Hasta" vaciaba la galeria. La fecha la
sirve el indice junto al listado de la carpeta, sin releer nada, y solo se usa
si la foto no ha cambiado desde que se indexo; si de esa foto aun no se sabe
nada, se recurre a la del nombre (si se renombro por EXIF) y por ultimo a la
del archivo.

### Cuando la fecha no es una fecha

Un campo EXIF puede traer texto con un prefijo de codificacion delante
(`ASCII\0\0\0...`), y ahi ExifTool corta el valor en el byte nulo y devuelve
literalmente `ASCII`. Un archivo con los metadatos tocados puede devolver
cualquier otra cosa donde deberia ir la fecha. Por eso la app da por buena una
fecha solo si tiene forma de fecha; si no la tiene:

- no se guarda en el indice ni se ensena en la interfaz,
- la fila del indice se marca para releer, asi que una fila envenenada no se
  queda para siempre,
- y se vuelve a leer el archivo, que es lo que se hacia antes de que existiera
  el indice.

Lo mismo con el renombrado: un valor que no es una fecha no puede acabar en el
nombre del archivo.

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
grupo se abren ESAS fotos como un album: la galeria muestra solo las de ese
punto (aunque esten en carpetas distintas) con todas las herramientas de
siempre, y las migas dicen "Mapa > N fotos aqui" para volver. El album se
guarda como el recuadro de coordenadas del grupo, no como una lista de rutas,
asi que sobrevive a renombrar, editar o cambiar fechas: cambia el nombre del
archivo, pero no el sitio donde se hizo la foto. Pulsando una foto suelta se ve
su miniatura y se puede abrir en el visor; para acercarse estan los botones
+ / -, la rueda y el pellizco. Las imagenes del mapa las descarga el navegador, asi que el dispositivo
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
