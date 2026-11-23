#!/usr/bin/env bash
# Conclui a troca de armazenamento (passo 4 do armazenamento-barato.sh): liga o contêiner à conta nova e religa a Byte.
# A Azure não deixa mudar a conta de uma ligação já existente ("dados"), então este script cria uma ligação NOVA no
# ambiente e troca só o volume do contêiner (uma revisão nova, com os mesmos segredos e variáveis). Rode no Cloud Shell:
#
#   bash armazenamento-concluir.sh                  # acha sozinho a conta nova (bytedados + números)
#   bash armazenamento-concluir.sh bytedados119617  # ou diga qual
set -euo pipefail
RG="${RG:-byte-rg}"
APP="${APP:-byte}"
SHARE="${SHARE:-byte-dados}"
say() { printf '\n\033[1m[armazenamento] %s\033[0m\n' "$*"; }
az account show -o none 2>/dev/null || { echo "Abra o Cloud Shell (bash) do portal da Azure."; exit 1; }

NEW="${1:-${NEW_SA:-}}"
if [ -z "$NEW" ]; then
  NEW="$(az storage account list -g "$RG" --query "[?kind=='StorageV2' && starts_with(name, 'bytedados')].name | [0]" -o tsv)"
fi
[ -n "$NEW" ] || { echo "Não achei a conta nova (StorageV2 bytedados...). Rode: bash armazenamento-concluir.sh <nome>"; exit 1; }
ENV_ID="$(az containerapp show -g "$RG" -n "$APP" --query properties.managedEnvironmentId -o tsv)"
ENV_NAME="${ENV_ID##*/}"
STORE="${STORE:-dados${NEW#bytedados}}"
say "Conta nova: $NEW · ligação no ambiente: $STORE · ambiente: $ENV_NAME"

say "1/3 Ligando a conta $NEW ao ambiente (como '$STORE')"
KEY="$(az storage account keys list -g "$RG" -n "$NEW" --query "[0].value" -o tsv)"
az containerapp env storage set -g "$RG" -n "$ENV_NAME" --storage-name "$STORE" --azure-file-account-name "$NEW" \
   --azure-file-account-key "$KEY" --azure-file-share-name "$SHARE" --access-mode ReadWrite -o none
unset KEY

say "2/3 Trocando o volume do contêiner para '$STORE' e religando (revisão nova; segredos e variáveis iguais)"
APP_ID="$(az containerapp show -g "$RG" -n "$APP" --query id -o tsv)"
BODY="$(az containerapp show -g "$RG" -n "$APP" --query properties.template -o json | STORE="$STORE" python3 -c '
import json, os, sys
t = json.load(sys.stdin)
t.pop("revisionSuffix", None)                       # sufixo repetido daria conflito com a revisão antiga
changed = 0
for v in t.get("volumes") or []:
    if v.get("storageType", "AzureFile") == "AzureFile" and v.get("storageName"):
        v["storageName"] = os.environ["STORE"]
        changed += 1
if not changed:
    sys.exit("o contêiner não tem volume do Azure Files")
print(json.dumps({"properties": {"template": t}}))
')"
az rest --method patch --url "https://management.azure.com${APP_ID}?api-version=2024-03-01" --body "$BODY" -o none

say "3/3 Esperando a Byte ligar (até 4 minutos)"
for _ in $(seq 1 24); do
  sleep 10
  STATE="$(az containerapp revision list -g "$RG" -n "$APP" \
           --query "[?properties.active] | [0].[name, properties.runningState]" -o tsv 2>/dev/null || true)"
  echo "    $STATE"
  case "$STATE" in *Running*|*running*) break ;; esac
done
VOL="$(az containerapp show -g "$RG" -n "$APP" --query "properties.template.volumes[0].storageName" -o tsv)"
OLD="$(az containerapp env storage show -g "$RG" -n "$ENV_NAME" --storage-name dados \
        --query properties.azureFile.accountName -o tsv 2>/dev/null || true)"

cat <<EOF

Volume do contêiner agora: $VOL (conta $NEW).
Confira no Telegram: /status e /agenda (suas tarefas e lembretes têm que estar lá).

Deu tudo certo? Então apague a conta antiga (é ela que custa caro; não tem volta):
    az storage account delete -g $RG -n ${OLD:-bytedadosgabriel} --yes
EOF
