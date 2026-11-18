#!/bin/sh
# Partida da Byte num contêiner (Azure Container Apps, imagem python:3.12-slim).
# Fica no armazenamento persistente (/mnt/dados) junto com o zip da Byte; o contêiner roda:
#     /bin/sh /mnt/dados/conteiner-iniciar.sh
# Instala a versão mais nova encontrada (byte-agent*.zip) e entrega para o supervisor (byte_agent.nuvem).
set -e
DADOS="${BYTE_DADOS_PERSISTENTES:-/mnt/dados}"
ZIP=$(ls -t "$DADOS"/byte-agent*.zip 2>/dev/null | head -1)
if [ -z "$ZIP" ]; then
  echo "[NUVEM] Nenhum byte-agent*.zip em $DADOS: envie o zip da Byte para o compartilhamento de arquivos." >&2
  sleep 300
  exit 1
fi
echo "[NUVEM] Instalando $(basename "$ZIP")..."
pip install -q --no-cache-dir --disable-pip-version-check "${ZIP}[trabalho]" || pip install -q --no-cache-dir --disable-pip-version-check "$ZIP"
exec python -m byte_agent.nuvem
