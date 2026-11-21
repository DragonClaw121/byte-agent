#!/usr/bin/env bash
# Liga a memória "por significado" dos agentes: cria a implantação do modelo de embeddings (text-embedding-3-small)
# no mesmo recurso da Azure OpenAI que a Byte já usa e grava BYTE_AZURE_EMBED no contêiner. Rode no Cloud Shell:
#
#   bash memoria-azure.sh
#
# Custo: centavos por mês (US$ 0,02 por milhão de tokens). As memórias antigas são reindexadas sozinhas, aos poucos.
set -euo pipefail
RG="${RG:-byte-rg}"
APP="${APP:-byte}"
DEPLOY="${DEPLOY:-byte-embed}"
say() { printf '\n\033[1m[memória] %s\033[0m\n' "$*"; }
az account show -o none 2>/dev/null || { echo "Abra o Cloud Shell (bash) do portal da Azure."; exit 1; }

EP="$(az containerapp show -g "$RG" -n "$APP" --query "properties.template.containers[0].env[?name=='AZURE_OPENAI_ENDPOINT'].value | [0]" -o tsv)"
EP="${EP%/}"
[ -n "$EP" ] || { echo "Não achei AZURE_OPENAI_ENDPOINT no contêiner $APP."; exit 1; }
read -r ACC ACC_RG < <(az cognitiveservices account list \
  --query "[?starts_with(properties.endpoint, '$EP')].[name, resourceGroup] | [0]" -o tsv)
[ -n "${ACC:-}" ] || { echo "Não achei o recurso da Azure OpenAI com o endereço $EP nesta assinatura."; exit 1; }
say "Recurso: $ACC (grupo $ACC_RG)"

if az cognitiveservices account deployment show -g "$ACC_RG" -n "$ACC" --deployment-name "$DEPLOY" -o none 2>/dev/null; then
  echo "    a implantação $DEPLOY já existe"
else
  say "Criando a implantação $DEPLOY (text-embedding-3-small)"
  az cognitiveservices account deployment create -g "$ACC_RG" -n "$ACC" --deployment-name "$DEPLOY" \
      --model-name text-embedding-3-small --model-version 1 --model-format OpenAI \
      --sku-name GlobalStandard --sku-capacity 100 -o none 2>/dev/null \
  || az cognitiveservices account deployment create -g "$ACC_RG" -n "$ACC" --deployment-name "$DEPLOY" \
      --model-name text-embedding-3-small --model-version 1 --model-format OpenAI \
      --sku-name Standard --sku-capacity 100 -o none
fi

say "Ligando a memória nova no contêiner (reinicia em ~1 minuto)"
az containerapp update -g "$RG" -n "$APP" --set-env-vars "BYTE_AZURE_EMBED=$DEPLOY" -o none
say "Pronto. Confira no painel (status → memoria) ou peça: 'o que você lembra sobre mim?'"
