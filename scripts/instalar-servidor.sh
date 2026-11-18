#!/usr/bin/env bash
# Prepara um servidor Ubuntu (ex.: VM da Azure) para rodar a Byte 24/7.
#
#   bash ~/byte-agent/scripts/instalar-servidor.sh
#
# Faz: pacotes do sistema, memória extra (swap) em máquinas de 1 GB, ambiente Python, modelo do ~/.byte-env,
# serviços do systemd (byte = Matrix/Byte/Bit na porta 8765; byte-kronus = Kronus na 8772) e o Tailscale.
# Pode rodar de novo a qualquer momento (por exemplo, depois de atualizar): ele só refaz o que faltar.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"
ENV_FILE="$HOME/.byte-env"
echo "[servidor] Projeto em $DIR, usuário $USER_NAME"

echo "[servidor] 1/6 Pacotes do sistema"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip git curl unzip qrencode >/dev/null

echo "[servidor] 2/6 Memória extra (swap)"
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$mem_mb" -lt 3000 ] && ! swapon --show | grep -q .; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo "[servidor]     2 GB de swap criados (máquina com ${mem_mb} MB de RAM)"
else
  echo "[servidor]     ok"
fi

echo "[servidor] 3/6 Ambiente Python"
cd "$DIR"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip >/dev/null 2>&1 || true
if ! .venv/bin/pip install -q -e ".[trabalho]" > /tmp/byte-pip.log 2>&1; then
  echo "[servidor]     ERRO ao instalar dependências; veja /tmp/byte-pip.log"; exit 1
fi

echo "[servidor] 4/6 Segredos ($ENV_FILE)"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
# Preencha e depois rode: sudo systemctl restart byte byte-kronus
# --- Cérebro na Azure OpenAI (portal da Azure > recurso > Chaves e Ponto de Extremidade) ---
export BYTE_MODEL="azure:byte-mini"
export AZURE_OPENAI_ENDPOINT="https://SEU-RECURSO.openai.azure.com"
export AZURE_OPENAI_API_KEY="PREENCHA"
# --- Liga as mensagens por iniciativa própria no servidor ---
export BYTE_AUTONOMIA=1
# --- Painel com link fixo ---
export BYTE_UI_TOKEN="PREENCHA"
# --- Telegram, voz, pesquisa e Kronus: copie do ~/.byte-env do notebook ---
export BYTE_TELEGRAM_TOKEN=""
export BYTE_TELEGRAM_CHAT_IDS=""
export BYTE_STT_URL="https://api.groq.com/openai/v1"
export BYTE_STT_MODEL="whisper-large-v3-turbo"
export BYTE_STT_API_KEY=""
export TAVILY_API_KEY=""
export KRONUS_API_TOKEN=""
EOF
  echo "[servidor]     modelo criado: preencha os valores (nano $ENV_FILE)"
fi
chmod 600 "$ENV_FILE"

echo "[servidor] 5/6 Serviços (ligam sozinhos com a máquina e voltam se caírem)"
unit() {   # $1 = nome, $2 = descrição, $3 = comando do byte.sh
  sudo tee "/etc/systemd/system/$1.service" >/dev/null <<EOF
[Unit]
Description=$2
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$DIR
Environment=BYTE_SERVICO=1
ExecStart=/bin/bash $DIR/scripts/byte.sh $3
KillSignal=SIGINT
TimeoutStopSec=20
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF
}
unit byte "Byte: Matrix, Byte e Bit (porta 8765)" iniciar
unit byte-kronus "Byte: Kronus dedicado (porta 8772)" kronus
sudo systemctl daemon-reload
sudo systemctl enable byte byte-kronus >/dev/null 2>&1
if grep -q "PREENCHA\|SEU-RECURSO" "$ENV_FILE"; then
  echo "[servidor]     serviços instalados, mas NÃO ligados: preencha $ENV_FILE e rode: sudo systemctl restart byte byte-kronus"
else
  sudo systemctl restart byte byte-kronus
  echo "[servidor]     ligados. Acompanhe com: journalctl -u byte -f"
fi
grep -qs "alias byte=" "$HOME/.bashrc" || echo "alias byte='bash $DIR/scripts/byte.sh'" >> "$HOME/.bashrc"

echo "[servidor] 6/6 Tailscale"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
fi
if tailscale status >/dev/null 2>&1; then
  echo "[servidor]     já conectado: $(tailscale status --self --peers=false 2>/dev/null | awk '{print $2}')"
else
  echo "[servidor]     rode: sudo tailscale up --ssh   (abra o link e entre na sua conta)"
fi

cat <<EOF

Pronto. Próximos passos:
  1. nano $ENV_FILE                         (preencha; copie do notebook o que já tinha)
  2. sudo systemctl restart byte byte-kronus
  3. sudo tailscale up --ssh                 (se ainda não fez)
  4. sudo tailscale serve --bg 8772          (página /voz do Kronus no celular)
  5. byte status                             (ou: journalctl -u byte -f)
EOF
