#!/bin/sh
# =============================================================================
# Actualiza Geotagger en el NAS y COMPRUEBA que se ha actualizado de verdad.
#
# El script anterior daba "listo" pasara lo que pasara: si el pull fallaba (o
# se cancelaba con Ctrl+C), seguia adelante, reiniciaba el contenedor con la
# imagen VIEJA y terminaba con un tick verde. Aqui cada paso puede parar el
# despliegue, y al final se le pregunta a la aplicacion que esta corriendo que
# version tiene. Si no responde con el commit que se acaba de construir, esto
# falla, y lo dice.
#
#   sh actualizar.sh              actualiza a lo ultimo de la rama actual
#   sh actualizar.sh --forzar     reconstruye aunque no haya cambios
#   sh actualizar.sh --verificar  no toca nada: solo dice que hay corriendo
# =============================================================================
set -eu

REPO="${GEOTAGGER_REPO:-/share/CACHEDEV1_DATA/Geotagger}"
SERVICIO="geotagger"
PUERTO="${GEOTAGGER_PUERTO:-5000}"
ESPERA="${GEOTAGGER_ESPERA:-90}"   # segundos como mucho esperando a que levante

# El aviso "Error loading config file: .docker/config.json: permission denied"
# es de QNAP: docker intenta leer la config del usuario y no puede. Es
# inofensivo, pero ensucia la salida y esconde los errores de verdad. Con un
# DOCKER_CONFIG propio y escribible desaparece.
DOCKER_CONFIG="${DOCKER_CONFIG:-/tmp/.docker-geotagger}"
export DOCKER_CONFIG
mkdir -p "$DOCKER_CONFIG"

rojo()  { printf '\033[31m%s\033[0m\n' "$*"; }
verde() { printf '\033[32m%s\033[0m\n' "$*"; }
info()  { printf '%s\n' "$*"; }
morir() { rojo "❌ $*"; rojo "   El contenedor se queda como estaba."; exit 1; }

# Container Station trae unas veces "docker compose" y otras "docker-compose".
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif docker-compose version >/dev/null 2>&1; then
  DC="docker-compose"
else
  morir "no encuentro ni 'docker compose' ni 'docker-compose'."
fi

# Pregunta a la aplicacion que esta atendiendo que version tiene. Se intenta
# desde fuera (curl) y, si aqui no hay curl, desde dentro del contenedor.
version_corriendo() {
  campo="$1"
  if command -v curl >/dev/null 2>&1; then
    salida=$(curl -fsS --max-time 5 "http://127.0.0.1:${PUERTO}/api/version" 2>/dev/null) || return 1
  else
    salida=$(docker exec "$SERVICIO" python -c \
      "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:5000/api/version',timeout=5).read().decode())" \
      2>/dev/null) || return 1
  fi
  printf '%s' "$salida" | sed -n "s/.*\"${campo}\"[ ]*:[ ]*\"\([^\"]*\)\".*/\1/p" | head -1
}

estado() {
  c=$(version_corriendo commit || true)
  if [ -z "${c:-}" ]; then
    info "   No responde /api/version."
    info "   O no esta levantado, o corre una version anterior a este cambio."
    docker ps --filter "name=^${SERVICIO}$" --format '   contenedor: {{.Status}} ({{.Image}})' || true
    return 1
  fi
  info "   commit:   $c"
  info "   rama:     $(version_corriendo branch || echo '?')"
  info "   built:    $(version_corriendo built || echo '?')"
  info "   arrancado: $(version_corriendo started || echo '?')"
}

cd "$REPO" 2>/dev/null || morir "no existe el repositorio en $REPO (ajusta GEOTAGGER_REPO)."
[ -d .git ] || morir "$REPO no es un repositorio git."

if [ "${1:-}" = "--verificar" ]; then
  info "🔎 Version en marcha:"
  estado || exit 1
  exit 0
fi

FORZAR=0
[ "${1:-}" = "--forzar" ] && FORZAR=1

RAMA=$(git rev-parse --abbrev-ref HEAD)
ANTES=$(git rev-parse HEAD)
info "⬇️  Actualizando $REPO (rama $RAMA)..."

# Con cambios locales sin guardar, el pull se para a la mitad y deja el arbol a
# medias. Mejor no empezar.
if [ -n "$(git status --porcelain)" ]; then
  git status --short
  morir "hay cambios locales sin guardar en $REPO."
fi

git fetch --prune origin "$RAMA" || morir "no se pudo contactar con GitHub (¿red? ¿credenciales?)."

# --ff-only: o avanza limpio, o falla. Nunca deja un merge a medio hacer.
git merge --ff-only "origin/$RAMA" || \
  morir "la rama local ha divergido de origin/$RAMA; resuelvelo a mano."

DESPUES=$(git rev-parse HEAD)
CORTO=$(git rev-parse --short HEAD)

if [ "$ANTES" = "$DESPUES" ]; then
  info "   Ya estaba en el ultimo commit ($CORTO)."
  if [ "$FORZAR" -eq 0 ]; then
    # Aun sin cambios, hay que confirmar que lo que CORRE es esto mismo: el
    # despliegue anterior pudo quedarse a medias.
    info "🔎 Comprobando que es lo que esta corriendo..."
    if [ "$(version_corriendo commit || true)" = "$DESPUES" ]; then
      verde "✅ El NAS ya esta corriendo $CORTO. No hay nada que hacer."
      exit 0
    fi
    info "   Lo que corre NO coincide con el repositorio: reconstruyendo."
  fi
else
  info "   $(git rev-parse --short "$ANTES") -> $CORTO"
  git --no-pager log --oneline "$ANTES..$DESPUES" | sed 's/^/     /'
fi

GIT_COMMIT="$DESPUES"
GIT_BRANCH="$RAMA"
BUILD_DATE=$(date '+%Y-%m-%d %H:%M:%S')
export GIT_COMMIT GIT_BRANCH BUILD_DATE

info "🔨 Construyendo la imagen..."
$DC build || morir "fallo la construccion de la imagen."

info "🔄 Levantando el contenedor..."
$DC up -d || morir "fallo al levantar el contenedor."

info "⏳ Esperando a que responda (hasta ${ESPERA}s)..."
i=0
while [ "$i" -lt "$ESPERA" ]; do
  if [ "$(version_corriendo commit || true)" = "$GIT_COMMIT" ]; then
    verde "✅ Actualizado y verificado: corriendo $CORTO ($RAMA)."
    exit 0
  fi
  i=$((i + 2))
  sleep 2
done

rojo "❌ Ha pasado el tiempo y la aplicacion no responde con el commit $CORTO."
info "   Lo que hay ahora mismo:"
estado || true
info "   Ultimas lineas del contenedor:"
docker logs --tail 40 "$SERVICIO" 2>&1 | sed 's/^/     /' || true
exit 1
