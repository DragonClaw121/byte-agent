#!/usr/bin/env bash
# Põe um agente para trabalhar à parte, o tempo todo (cada um no seu processo, com bot do Telegram próprio).
# Rode no Cloud Shell (bash):
#
#   bash agentes-separados.sh bit        # o Bit (trabalho)
#   bash agentes-separados.sh byte       # a Byte (VTuber; só faz sentido quando a live estiver pronta)
#
# Antes: crie o bot no @BotFather (/newbot) e copie o token. O token é pedido sem aparecer na tela e vai direto para os
# Segredos do contêiner. O contêiner passa para 0,5 vCPU e 1 GiB (quatro agentes juntos não cabem em 0,5 GiB).
set -euo pipefail
RG="${RG:-byte-rg}"
APP="${APP:-byte}"
AGENT="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
case "$AGENT" in bit|byte) ;; *) echo "Use: bash agentes-separados.sh bit   (ou byte)"; exit 1 ;; esac
say() { printf '\n\033[1m[%s] %s\033[0m\n' "$AGENT" "$*"; }
az account show -o none 2>/dev/null || { echo "Abra o Cloud Shell (bash) do portal da Azure."; exit 1; }
env_value() {
  az containerapp show -g "$RG" -n "$APP" --query "properties.template.containers[0].env[?name=='$1'].value | [0]" -o tsv 2>/dev/null || true
}

if [ "$AGENT" = bit ]; then TOKEN_VAR=BIT_TELEGRAM_TOKEN; CHATS_VAR=BIT_TELEGRAM_CHAT_IDS; SECRET=bit-telegram
else TOKEN_VAR=VTUBER_TELEGRAM_TOKEN; CHATS_VAR=VTUBER_TELEGRAM_CHAT_IDS; SECRET=vtuber-telegram; fi

TOKEN=""
while [ -z "$TOKEN" ]; do
  read -rsp "    Token do bot do Telegram de ${AGENT^} (do @BotFather; não aparece): " TOKEN; echo
  TOKEN="${TOKEN//[$'\r\n\t ']/}"
done
case "$TOKEN" in *:*) echo "      ✓ recebido (${#TOKEN} caracteres)" ;; *) echo "      aviso: o token costuma ter ':' no meio." ;; esac
CHATS="$(env_value BYTE_TELEGRAM_CHAT_IDS)"
read -rp "    Seu chat_id do Telegram [${CHATS:-?}]: " IN
CHATS="${IN:-$CHATS}"
[ -n "$CHATS" ] || { echo "Informe o chat_id (o mesmo que você usa com a Matrix)."; exit 1; }

say "Guardando o token nos Segredos"
az containerapp secret set -g "$RG" -n "$APP" --secrets "$SECRET=$TOKEN" -o none
unset TOKEN

AGENTES="$(env_value BYTE_AGENTES)"
AGENTES="${AGENTES:-kronus}"
printf '%s' "$AGENTES" | tr ',' '\n' | grep -qx "$AGENT" || AGENTES="$AGENTES,$AGENT"

say "Ligando (${AGENTES}) e aumentando o contêiner para 0,5 vCPU / 1 GiB; reinicia em ~1 minuto"
az containerapp update -g "$RG" -n "$APP" --cpu 0.5 --memory 1.0Gi \
  --set-env-vars "BYTE_AGENTES=$AGENTES" "$TOKEN_VAR=secretref:$SECRET" "$CHATS_VAR=$CHATS" -o none
say "Pronto."
echo "    Abra o bot novo no Telegram e mande /start. ${AGENT^} agora trabalha sozinho, o tempo todo."
echo "    O link do painel dele aparece nos logs: az containerapp logs show -g $RG -n $APP --tail 80 | grep Painel"
