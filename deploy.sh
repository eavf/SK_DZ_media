#!/usr/bin/env bash
# deploy.sh — zbuilduje image, pushne na Docker Hub, nasadí na Synology
# Použitie:
#   ./deploy.sh              — full deploy (build + push + scp + pull na Synology)
#   ./deploy.sh --no-rebuild — skopíruje templates/config/static a reštartuje
#   ./deploy.sh --tpl-only   — len šablóny (bez reštartu, hneď aktívne)
#   ./deploy.sh --restart    — len reštartuje kontajner na Synology
#   ./deploy.sh --rebuild    — len build + push (bez deployu na Synology)

set -euo pipefail

DOCKER_IMAGE="eavfeavf/dz-news:latest"
SYNOLOGY_HOST="synology"   # alias z ~/.ssh/config
REMOTE_DIR="/volume1/docker/dz_news"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
SCP="scp"
SSH="ssh -t ${SYNOLOGY_HOST}"
DOCKER="sudo /usr/local/bin/docker"

MODE="full"
case "${1:-}" in
  --tpl-only)    MODE="tpl" ;;
  --no-rebuild)  MODE="no-rebuild" ;;
  --restart)     MODE="restart" ;;
  --rebuild)     MODE="rebuild" ;;
  -h|--help)
    echo "Použitie: ./deploy.sh [MOŽNOSŤ]"
    echo ""
    echo "  (bez možnosti)   Full deploy: build image → push Docker Hub → scp konfig → pull + restart na Synology"
    echo "  --no-rebuild     Skopíruje config/static na Synology a reštartuje (bez buildu)"
    echo "  --tpl-only       Len šablóny (templates/) na Synology, bez reštartu — zmeny sú hneď aktívne"
    echo "  --restart        Len reštartuje kontajnery na Synology"
    echo "  --rebuild        Len build + push na Docker Hub (bez nasadenia na Synology)"
    echo "  -h, --help       Zobrazí túto nápovedu"
    exit 0
    ;;
esac

if [[ $MODE == "tpl" ]]; then
  echo "--> Kopírujem len šablóny..."
  ${SCP} -r "${LOCAL_DIR}/templates" "${SYNOLOGY_HOST}:${REMOTE_DIR}/"
  echo "==> Hotovo. Obnov stránku v prehliadači."
  exit 0
fi

if [[ $MODE == "no-rebuild" ]]; then
  echo "--> Kopírujem config a static (bez rebuildu)..."
  tar -czf - --exclude='__pycache__' --exclude='*.pyc' -C "${LOCAL_DIR}" config | \
    ssh "${SYNOLOGY_HOST}" "tar -xzf - -C ${REMOTE_DIR} && chmod 666 ${REMOTE_DIR}/config/*.json"
  ${SCP} -r "${LOCAL_DIR}/static"    "${SYNOLOGY_HOST}:${REMOTE_DIR}/"
  echo "--> Rešartujem kontajner..."
  ${SSH} "cd ${REMOTE_DIR} && ${DOCKER} compose restart"
  echo "==> Hotovo."
  exit 0
fi

if [[ $MODE == "restart" ]]; then
  echo "--> Rešartujem kontajner na Synology..."
  ${SSH} "cd ${REMOTE_DIR} && ${DOCKER} compose up --force-recreate -d"
  echo "==> Hotovo."
  exit 0
fi

if [[ $MODE == "rebuild" ]]; then
  echo "--> Buildujem Docker image..."
  docker build -t "${DOCKER_IMAGE}" "${LOCAL_DIR}"
  echo "--> Pushnem na Docker Hub..."
  docker push "${DOCKER_IMAGE}"
  echo "==> Image pushnutý. Nasaď ho cez: ./deploy.sh --restart"
  exit 0
fi

# ── 1. Build image ─────────────────────────────────────────────────────────────
echo "--> Buildujem Docker image..."
docker build -t "${DOCKER_IMAGE}" "${LOCAL_DIR}"

# ── 2. Push na Docker Hub ──────────────────────────────────────────────────────
echo "--> Pushnem na Docker Hub..."
docker push "${DOCKER_IMAGE}"

# ── 3. Skopíruj konfig a šablóny na Synology ──────────────────────────────────
echo "--> Kopírujem konfig na Synology..."

${SCP} \
  "${LOCAL_DIR}/docker-compose.yml" \
  "${SYNOLOGY_HOST}:${REMOTE_DIR}/"

${SCP} -r "${LOCAL_DIR}/templates" "${SYNOLOGY_HOST}:${REMOTE_DIR}/"
tar -czf - --exclude='__pycache__' --exclude='*.pyc' -C "${LOCAL_DIR}" config | \
  ssh "${SYNOLOGY_HOST}" "tar -xzf - -C ${REMOTE_DIR} && chmod 666 ${REMOTE_DIR}/config/*.json"
${SCP} -r "${LOCAL_DIR}/static"    "${SYNOLOGY_HOST}:${REMOTE_DIR}/"

# ── 4. Nastav práva na zapisovateľné adresáre ─────────────────────────────────
echo "--> Nastavujem práva..."
${SSH} "sudo /bin/mkdir -p ${REMOTE_DIR}/logs ${REMOTE_DIR}/data ${REMOTE_DIR}/bundle && \
  sudo /bin/chmod 777 \
  ${REMOTE_DIR}/logs \
  ${REMOTE_DIR}/data \
  ${REMOTE_DIR}/bundle \
  ${REMOTE_DIR}/templates \
  ${REMOTE_DIR}/config \
  ${REMOTE_DIR}/static"

# ── 5. Pull nový image a reštartuj ────────────────────────────────────────────
echo "--> Pullnem nový image a reštartujem na Synology..."
${SSH} "cd ${REMOTE_DIR} && ${DOCKER} compose pull && ${DOCKER} compose up --force-recreate -d && ${DOCKER} image prune -f"
echo "==> Deploy hotový."