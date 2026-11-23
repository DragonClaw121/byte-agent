#!/bin/sh
# Partida da Byte num contêiner (Azure Container Apps, imagem python:3.12-slim).
# Fica no armazenamento persistente (/mnt/dados) junto com o zip da Byte; o contêiner roda:
#     /bin/sh /mnt/dados/conteiner-iniciar.sh
# Instala a Byte e entrega para o supervisor (byte_agent.nuvem). Prefere a versão baixada do GitHub pelo atualizador
# (byte-agent-github.zip, sempre a mais nova com testes verdes); sem ela, o byte-agent*.zip mais recente.
# (A data dos arquivos não é confiável: copiar o compartilhamento para outra conta muda todas.)
set -e
DADOS="${BYTE_DADOS_PERSISTENTES:-/mnt/dados}"
if [ -f "$DADOS/byte-agent-github.zip" ]; then
  ZIP="$DADOS/byte-agent-github.zip"
else
  ZIP=$(ls -t "$DADOS"/byte-agent*.zip 2>/dev/null | head -1)
fi
if [ -z "$ZIP" ]; then
  echo "[NUVEM] Nenhum byte-agent*.zip em $DADOS: envie o zip da Byte para o compartilhamento de arquivos." >&2
  sleep 300
  exit 1
fi
echo "[NUVEM] Instalando $(basename "$ZIP")..."
pip install -q --no-cache-dir --disable-pip-version-check "${ZIP}[trabalho]" || pip install -q --no-cache-dir --disable-pip-version-check "$ZIP"
exec python -m byte_agent.nuvem
