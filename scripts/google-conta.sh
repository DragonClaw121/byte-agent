#!/usr/bin/env bash
# Liga o Google Agenda e o Google Drive nos agentes. Rode no Cloud Shell da AZURE (bash), em duas etapas:
#
#   bash google-conta.sh          1ª vez: mostra o "URI de redirecionamento" para cadastrar no Google Cloud;
#                                 depois pede o ID do cliente e a chave secreta que o Google gerar.
#
# Passo a passo do Google Cloud: docs/google.md. A chave secreta é pedida sem aparecer na tela e vai direto para os
# Segredos do contêiner.
set -euo pipefail
RG="${RG:-byte-rg}"
APP="${APP:-byte}"
say() { printf '\n\033[1m[google] %s\033[0m\n' "$*"; }
az account show -o none 2>/dev/null || { echo "Abra o Cloud Shell (bash) do portal da Azure."; exit 1; }

FQDN="$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)"
[ -n "$FQDN" ] || { echo "Não achei o endereço público do contêiner $APP."; exit 1; }
REDIRECT="https://$FQDN/oauth/google"
say "Cadastre este endereço no Google Cloud (cliente OAuth → URIs de redirecionamento autorizados):"
echo
echo "    $REDIRECT"
echo
read -rp "    Já criou o cliente OAuth com esse endereço? Cole o ID do cliente (termina em .apps.googleusercontent.com): " CID
CID="$(printf '%s' "$CID" | tr -d '[:space:]')"
case "$CID" in *.apps.googleusercontent.com) ;; *) echo "Esse não parece um ID do cliente do Google."; exit 1 ;; esac
SECRET=""
while [ -z "$SECRET" ]; do
  read -rsp "    Chave secreta do cliente (começa com GOCSPX-; não aparece): " SECRET; echo
  SECRET="${SECRET//[$'\r\n\t ']/}"
done
echo "      ✓ recebida (${#SECRET} caracteres)"

say "Guardando nos Segredos"
az containerapp secret set -g "$RG" -n "$APP" --secrets "google-segredo=$SECRET" -o none
unset SECRET
if [ -z "$(az containerapp secret list -g "$RG" -n "$APP" --query "[?name=='chave-tokens'].name" -o tsv 2>/dev/null)" ]; then
  az containerapp secret set -g "$RG" -n "$APP" --secrets "chave-tokens=$(openssl rand -base64 32)" -o none
fi

say "Aplicando (o contêiner reinicia em ~1 minuto)"
az containerapp update -g "$RG" -n "$APP" --set-env-vars "BYTE_GOOGLE_CLIENT_ID=$CID" \
  "BYTE_GOOGLE_CLIENT_SECRET=secretref:google-segredo" "BYTE_GOOGLE_REDIRECT=$REDIRECT" \
  "BYTE_CHAVE_TOKENS=secretref:chave-tokens" -o none
say "Pronto. No Telegram do agente (Bit, Matrix ou Kronus): /google entrar"
