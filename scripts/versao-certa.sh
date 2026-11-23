#!/usr/bin/env bash
# Faz o contêiner ligar com a versão mais nova da Byte (a baixada do GitHub), não com um zip antigo do armazenamento.
# Envia o conteiner-iniciar.sh novo (que prefere byte-agent-github.zip) e reinicia. Rode no Cloud Shell:
#
#   bash versao-certa.sh
set -euo pipefail
RG="${RG:-byte-rg}"
APP="${APP:-byte}"
SHARE="${SHARE:-byte-dados}"
REPO="${REPO:-DragonClaw121/byte-agent}"
say() { printf '\n\033[1m[versão] %s\033[0m\n' "$*"; }
az account show -o none 2>/dev/null || { echo "Abra o Cloud Shell (bash) do portal da Azure."; exit 1; }

ENV_NAME="$(az containerapp show -g "$RG" -n "$APP" --query properties.managedEnvironmentId -o tsv)"; ENV_NAME="${ENV_NAME##*/}"
STORE="$(az containerapp show -g "$RG" -n "$APP" --query "properties.template.volumes[0].storageName" -o tsv)"
SA="$(az containerapp env storage show -g "$RG" -n "$ENV_NAME" --storage-name "$STORE" --query properties.azureFile.accountName -o tsv)"
KEY="$(az storage account keys list -g "$RG" -n "$SA" --query "[0].value" -o tsv)"
say "Armazenamento em uso: $SA/$SHARE"

say "Versões da Byte guardadas no armazenamento"
az storage file list -s "$SHARE" --account-name "$SA" --account-key "$KEY" \
   --query "[?starts_with(name, 'byte-agent')].{arquivo:name, KB:properties.contentLength}" -o table
if ! az storage file exists -s "$SHARE" -p byte-agent-github.zip --account-name "$SA" --account-key "$KEY" \
       --query exists -o tsv | grep -q true; then
  echo "Não há byte-agent-github.zip no armazenamento: mande esta saída para o assistente."; exit 1
fi

say "Enviando o conteiner-iniciar.sh novo (prefere a versão do GitHub)"
TMP="$(mktemp)"
curl -fsSL "https://raw.githubusercontent.com/$REPO/main/scripts/conteiner-iniciar.sh" -o "$TMP"
grep -q "byte-agent-github.zip" "$TMP" || { echo "O arquivo baixado não é o esperado; tente de novo em 5 minutos."; exit 1; }
az storage file upload -s "$SHARE" --source "$TMP" -p conteiner-iniciar.sh --account-name "$SA" --account-key "$KEY" \
   --only-show-errors -o none
rm -f "$TMP"; unset KEY

say "Reiniciando a Byte (1 a 2 minutos)"
REV="$(az containerapp revision list -g "$RG" -n "$APP" --query "[?properties.active] | [0].name" -o tsv)"
az containerapp revision restart -g "$RG" -n "$APP" --revision "$REV" -o none
sleep 100
say "Versão que ligou:"
az containerapp logs show -g "$RG" -n "$APP" --tail 300 --format text 2>/dev/null \
  | grep -oE "Instalando [^ ]+|\[BYTE v[0-9.]+\]|Ligando [a-z]+" | sort -u || true
echo
echo "Esperado: 'Instalando byte-agent-github.zip', [BYTE v13.4x] e 'Ligando principal', 'Ligando kronus', 'Ligando bit'."
