#!/usr/bin/env bash
# Troca o armazenamento da Byte por um que cobra pelo USO (centavos por mês para poucos MB), no lugar da conta
# "FileStorage / provisionada v2", que cobra IOPS e banda mínimos reservados mesmo vazia. Rode no Cloud Shell:
#
#   bash armazenamento-barato.sh
#
# O que ele faz, nesta ordem (nada é apagado):
#   1. cria uma conta nova (StorageV2, Standard LRS) com o compartilhamento byte-dados (pago pelo uso);
#   2. para a Byte (ela salva os dados no armazenamento ao desligar);
#   3. copia TUDO do compartilhamento antigo para o novo (e guarda uma cópia aqui no Cloud Shell);
#   4. aponta o contêiner para o compartilhamento novo e liga a Byte de novo.
# No fim, mostra o comando para apagar a conta antiga: rode só depois de conferir que a Byte respondeu.
set -euo pipefail
RG="${RG:-byte-rg}"
APP="${APP:-byte}"
SHARE="${SHARE:-byte-dados}"
say() { printf '\n\033[1m[armazenamento] %s\033[0m\n' "$*"; }
az account show -o none 2>/dev/null || { echo "Abra o Cloud Shell (bash) do portal da Azure."; exit 1; }

ENV_ID="$(az containerapp show -g "$RG" -n "$APP" --query properties.managedEnvironmentId -o tsv)"
ENV_NAME="${ENV_ID##*/}"
OLD="$(az containerapp env storage show -g "$RG" -n "$ENV_NAME" --storage-name dados \
        --query properties.azureFile.accountName -o tsv)"
LOC="$(az storage account show -g "$RG" -n "$OLD" --query location -o tsv)"
OLD_KIND="$(az storage account show -g "$RG" -n "$OLD" --query kind -o tsv)"
say "Hoje: conta $OLD ($OLD_KIND), compartilhamento $SHARE, ambiente $ENV_NAME, região $LOC"
if [ "$OLD_KIND" = "StorageV2" ]; then
  echo "Essa conta já é do tipo que cobra pelo uso. Nada a fazer."; exit 0
fi

NEW="${NEW_SA:-}"
if [ -z "$NEW" ]; then
  for _ in 1 2 3 4 5; do
    cand="bytedados$((RANDOM % 900000 + 100000))"
    if [ "$(az storage account check-name -n "$cand" --query nameAvailable -o tsv)" = "true" ]; then NEW="$cand"; break; fi
  done
fi
[ -n "$NEW" ] || { echo "Não achei um nome livre; rode de novo com NEW_SA=<nome> bash armazenamento-barato.sh"; exit 1; }

say "1/4 Criando a conta $NEW (StorageV2, Standard LRS) e o compartilhamento $SHARE (pago pelo uso)"
if ! az storage account show -g "$RG" -n "$NEW" -o none 2>/dev/null; then
  az storage account create -g "$RG" -n "$NEW" -l "$LOC" --kind StorageV2 --sku Standard_LRS \
     --min-tls-version TLS1_2 --allow-blob-public-access false --https-only true --tags projeto=byte -o none
fi
if ! az storage share-rm show -g "$RG" --storage-account "$NEW" -n "$SHARE" -o none 2>/dev/null; then
  az storage share-rm create -g "$RG" --storage-account "$NEW" -n "$SHARE" --quota 5 \
     --access-tier TransactionOptimized -o none
fi
OLD_KEY="$(az storage account keys list -g "$RG" -n "$OLD" --query "[0].value" -o tsv)"
NEW_KEY="$(az storage account keys list -g "$RG" -n "$NEW" --query "[0].value" -o tsv)"

say "2/4 Parando a Byte (ela salva os dados ao desligar; leva até 1 minuto)"
REVS="$(az containerapp revision list -g "$RG" -n "$APP" --query "[?properties.active].name" -o tsv)"
for rev in $REVS; do
  az containerapp revision deactivate -g "$RG" -n "$APP" --revision "$rev" -o none
done
sleep 45

say "3/4 Copiando os dados de $OLD para $NEW"
COPIA="$HOME/byte-copia-$(date +%Y%m%d-%H%M)"
mkdir -p "$COPIA"
az storage file download-batch -s "$SHARE" -d "$COPIA" --account-name "$OLD" --account-key "$OLD_KEY" \
   --only-show-errors -o none
N_OLD="$(find "$COPIA" -type f | wc -l)"
az storage file upload-batch -d "$SHARE" -s "$COPIA" --account-name "$NEW" --account-key "$NEW_KEY" \
   --only-show-errors -o none
echo "    $N_OLD arquivos copiados (cópia de segurança também em $COPIA)"
[ "$N_OLD" -gt 0 ] || {
  echo "    Nada foi copiado: a Byte volta a usar a conta antiga."
  for rev in $REVS; do az containerapp revision activate -g "$RG" -n "$APP" --revision "$rev" -o none; done
  exit 1; }
[ -f "$COPIA/conteiner-iniciar.sh" ] || echo "    aviso: conteiner-iniciar.sh não estava na raiz do compartilhamento antigo"

say "4/4 Apontando o contêiner para $NEW e ligando a Byte"
unset OLD_KEY NEW_KEY
# A Azure não deixa trocar a conta da ligação "dados": o concluir cria uma ligação nova e troca o volume do contêiner.
CONCLUIR="$(dirname "$0")/armazenamento-concluir.sh"
[ -f "$CONCLUIR" ] || curl -s -o "$CONCLUIR" \
  https://raw.githubusercontent.com/DragonClaw121/byte-agent/main/scripts/armazenamento-concluir.sh
bash "$CONCLUIR" "$NEW"
