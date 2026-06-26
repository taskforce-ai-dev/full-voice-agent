#!/usr/bin/env bash
# =============================================================================
# deploy.sh -- KITCHENED' Voice Agent Deployment Script
# =============================================================================
# Usage:
#   ./deploy.sh setup   -- First-time server provisioning
#   ./deploy.sh deploy  -- Push code updates and restart
#   ./deploy.sh logs    -- Tail container logs
#   ./deploy.sh status  -- Check health and container status
# =============================================================================

set -euo pipefail

# ===================== EDIT THESE =====================
SERVER_IP="67.207.90.109"
DOMAIN="kitchened.taskforceai.tech"
EMAIL="info@taskforceai.tech"
# ======================================================

SERVER_USER="root"
REMOTE_DIR="/opt/kitchened"
SSH_TARGET="${SERVER_USER}@${SERVER_IP}"

# Production files to sync
PROD_FILES=(
  Dockerfile
  docker-compose.yml
  nginx.conf
  requirements-prod.txt
  .dockerignore
  .env.example
  server.py
  media_stream_server.py
  tools.py
  booking_api.py
  post_call.py
  knowledge_base.py
  yanolja_client.py
  yanolja_service.py
)

# --------------------------------------------------------------------------
# Helper functions
# --------------------------------------------------------------------------
info()  { echo -e "\033[1;34m[INFO]\033[0m $*"; }
ok()    { echo -e "\033[1;32m[OK]\033[0m $*"; }
err()   { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; }

run_remote() {
  ssh -o StrictHostKeyChecking=accept-new "$SSH_TARGET" "$@"
}

check_config() {
  if [[ "$SERVER_IP" == "YOUR_SERVER_IP" || "$DOMAIN" == "YOUR_DOMAIN" ]]; then
    err "Edit deploy.sh first — set SERVER_IP, DOMAIN, and EMAIL at the top."
    exit 1
  fi
}

# --------------------------------------------------------------------------
# setup — First-time server provisioning
# --------------------------------------------------------------------------
cmd_setup() {
  check_config
  info "Setting up $SERVER_IP for KITCHENED' deployment..."

  info "1/8 Updating system packages..."
  run_remote "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y"

  info "2/8 Installing Docker..."
  run_remote "bash -s" <<'DOCKER_INSTALL'
    if command -v docker &>/dev/null; then
      echo "Docker already installed, skipping."
    else
      apt-get install -y ca-certificates curl gnupg
      install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
      chmod a+r /etc/apt/keyrings/docker.gpg
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
      apt-get update
      apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
      systemctl enable docker
    fi
DOCKER_INSTALL

  info "3/8 Installing Nginx and Certbot..."
  run_remote "apt-get install -y nginx certbot python3-certbot-nginx && systemctl enable nginx"

  info "4/8 Configuring firewall..."
  run_remote "bash -s" <<'UFW_SETUP'
    ufw allow OpenSSH
    ufw allow 80/tcp
    ufw allow 443/tcp
    echo "y" | ufw enable || true
UFW_SETUP

  info "5/8 Uploading project files..."
  run_remote "mkdir -p ${REMOTE_DIR}"
  for f in "${PROD_FILES[@]}"; do
    scp "$f" "${SSH_TARGET}:${REMOTE_DIR}/"
  done
  scp -r knowledge_docs "${SSH_TARGET}:${REMOTE_DIR}/"

  info "6/8 Setting up .env..."
  if [[ -f .env ]]; then
    echo ""
    echo "  Found local .env — uploading to server."
    echo "  IMPORTANT: Verify the values are correct for production!"
    echo ""
    scp .env "${SSH_TARGET}:${REMOTE_DIR}/.env"
    run_remote "chmod 600 ${REMOTE_DIR}/.env"
  else
    err "No .env file found locally."
    echo "  Create ${REMOTE_DIR}/.env on the server manually:"
    echo "  ssh ${SSH_TARGET} 'cp ${REMOTE_DIR}/.env.example ${REMOTE_DIR}/.env && nano ${REMOTE_DIR}/.env'"
    echo ""
    read -rp "  Press Enter when .env is ready on the server..."
  fi

  info "7/8 Setting up SSL certificate..."
  # Temporary nginx config for certbot
  run_remote "bash -s" <<NGINX_TEMP
    cat > /etc/nginx/sites-available/kitchened <<'CONF'
server {
    listen 80;
    server_name ${DOMAIN};
    location / {
        return 200 'KITCHENED setup in progress';
        add_header Content-Type text/plain;
    }
}
CONF
    ln -sf /etc/nginx/sites-available/kitchened /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    nginx -t && systemctl reload nginx
NGINX_TEMP

  info "  Requesting SSL certificate for ${DOMAIN}..."
  run_remote "certbot --nginx -d ${DOMAIN} --non-interactive --agree-tos -m ${EMAIL}"

  # Install full nginx config
  run_remote "sed 's/YOUR_DOMAIN/${DOMAIN}/g' ${REMOTE_DIR}/nginx.conf > /etc/nginx/sites-available/kitchened && nginx -t && systemctl reload nginx"

  info "8/8 Building and starting Docker container..."
  run_remote "cd ${REMOTE_DIR} && docker compose build && docker compose up -d"

  info "Waiting 45 seconds for startup (model warm-up)..."
  sleep 45

  echo ""
  cmd_status
  echo ""
  ok "Setup complete!"
  echo ""
  echo "  Next steps:"
  echo "  1. Configure Twilio webhook: https://${DOMAIN}/voice/incoming (POST)"
  echo "  2. Make a test phone call"
  echo "  3. Monitor logs: ./deploy.sh logs"
  echo ""
}

# --------------------------------------------------------------------------
# deploy — Push code updates and restart
# --------------------------------------------------------------------------
cmd_deploy() {
  check_config
  info "Deploying code updates to ${SERVER_IP}..."

  for f in "${PROD_FILES[@]}"; do
    scp "$f" "${SSH_TARGET}:${REMOTE_DIR}/"
  done
  scp -r knowledge_docs "${SSH_TARGET}:${REMOTE_DIR}/"

  info "Rebuilding and restarting container..."
  run_remote "cd ${REMOTE_DIR} && docker compose build && docker compose up -d"

  info "Waiting 30 seconds for startup..."
  sleep 30

  cmd_status
  ok "Deploy complete!"
}

# --------------------------------------------------------------------------
# logs — Tail container logs
# --------------------------------------------------------------------------
cmd_logs() {
  check_config
  run_remote "cd ${REMOTE_DIR} && docker compose logs -f kitchened"
}

# --------------------------------------------------------------------------
# status — Health check and container status
# --------------------------------------------------------------------------
cmd_status() {
  check_config
  info "Checking health..."
  echo ""

  local health
  health=$(curl -sf "https://${DOMAIN}/health" 2>&1) || true
  if [[ -n "$health" ]]; then
    ok "Health: $health"
  else
    err "Health endpoint not reachable at https://${DOMAIN}/health"
  fi

  echo ""
  info "Container status:"
  run_remote "cd ${REMOTE_DIR} && docker compose ps" || true
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
case "${1:-help}" in
  setup)  cmd_setup  ;;
  deploy) cmd_deploy ;;
  logs)   cmd_logs   ;;
  status) cmd_status ;;
  *)
    echo "Usage: $0 {setup|deploy|logs|status}"
    echo ""
    echo "  setup   — First-time server provisioning (Docker, nginx, SSL, build)"
    echo "  deploy  — Push code updates and restart container"
    echo "  logs    — Tail container logs"
    echo "  status  — Check health and container status"
    exit 1
    ;;
esac
