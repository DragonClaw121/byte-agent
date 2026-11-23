#!/usr/bin/env bash
# Descobre por que o armazenamento da Byte está caro e, se você pedir, reduz ao mínimo. Rode no Cloud Shell:
#
#   bash custo-armazenamento.sh            # só mostra (não muda nada)
#   bash custo-armazenamento.sh reduzir    # reduz o compartilhamento ao mínimo que a Azure permite
#
# Os dados da Byte ocupam poucos MB. O que costuma pesar é o compartilhamento ter sido criado com capacidade
# PROVISIONADA (paga-se pelo tamanho/IOPS/banda reservados, mesmo vazios) ou na camada Premium.
set -uo pipefail
RG="${RG:-byte-rg}"
SHARE="${SHARE:-byte-dados}"
say() { printf '\n\033[1m[armazenamento] %s\033[0m\n' "$*"; }
az account show -o none 2>/dev/null || { echo "Abra o Cloud Shell (bash) do portal da Azure."; exit 1; }

SA="${SA:-$(az storage account list -g "$RG" --query "[0].name" -o tsv 2>/dev/null)}"
[ -n "$SA" ] || { echo "Não achei conta de armazenamento no grupo $RG."; exit 1; }

say "Conta de armazenamento: $SA"
az storage account show -g "$RG" -n "$SA" \
  --query "{tipo:kind, desempenho_e_redundancia:sku.name, camada:accessTier, regiao:location}" -o table

say "Compartilhamento $SHARE"
INFO="$(az storage share-rm show -g "$RG" --storage-account "$SA" -n "$SHARE" --expand stats -o json 2>/dev/null)"
if [ -z "$INFO" ]; then
  echo "Não achei o compartilhamento $SHARE."
else
  printf '%s' "$INFO" | python3 -c '
import json, sys
d = json.load(sys.stdin); p = d.get("properties", d)
def g(*names):
    for n in names:
        if p.get(n) is not None:
            return p[n]
        if d.get(n) is not None:
            return d[n]
    return None
used = g("shareUsageBytes") or 0
quota = g("shareQuota")
iops, bw = g("provisionedIops", "provisionedIOPS"), g("provisionedBandwidthMibps", "provisionedBandwidthMiBps")
print(f"  usado de verdade : {used / 1024 / 1024:.1f} MB")
print(f"  tamanho/cota     : {quota} GiB")
tier = g("accessTier") or "(provisionado: sem camada)"
print(f"  camada           : {tier}")
if iops or bw:
    print(f"  provisionado     : {iops} IOPS, {bw} MiB/s  <- cobrado mesmo sem uso")
'
fi

say "Discos e snapshots na assinatura inteira (sobras de máquinas virtuais cobram como 'Armazenamento')"
az disk list --query "[].{disco:name, grupo:resourceGroup, GiB:diskSizeGb, tipo:sku.name, estado:diskState}" -o table 2>/dev/null \
  || echo "  (não consegui listar os discos)"
az snapshot list --query "[].{snapshot:name, grupo:resourceGroup, GiB:diskSizeGb, tipo:sku.name}" -o table 2>/dev/null
SOLTOS="$(az disk list --query "[?diskState=='Unattached'].id" -o tsv 2>/dev/null)"
if [ -n "$SOLTOS" ]; then
  echo
  echo "  ⚠️  Há disco(s) SOLTO(S) (estado Unattached: não estão ligados a nenhuma máquina, mas são cobrados)."
  echo "  Se forem sobras das tentativas de máquina virtual, apague com (um por linha; não tem volta):"
  for id in $SOLTOS; do echo "    az disk delete --ids $id --yes"; done
fi

say "Contas de armazenamento na assinatura"
az storage account list --query "[].{conta:name, grupo:resourceGroup, tipo:kind, sku:sku.name}" -o table 2>/dev/null

say "Custo dos últimos 30 dias por recurso (pode demorar; em alguns tipos de assinatura não aparece)"
START="$(date -u -d '30 days ago' +%Y-%m-%d)"; END="$(date -u +%Y-%m-%d)"
az consumption usage list --start-date "$START" --end-date "$END" --include-meter-details -o json 2>/dev/null \
| python3 -c '
import json, sys, collections
try:
    rows = json.load(sys.stdin)
except Exception:
    print("  (a Azure não devolveu o detalhamento: veja em Gerenciamento de Custos > Análise de custo, agrupado por Recurso)")
    sys.exit(0)
def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
def first(r, *keys):
    for k in keys:
        v = r.get(k)
        if v not in (None, "", "None"):
            return v
    return ""
tot = collections.Counter()
cur = ""
for r in rows:
    m = r.get("meterDetails") or {}
    cost = 0.0
    for k in ("pretaxCost", "costInBillingCurrency", "cost", "paygCostInBillingCurrency", "costInPricingCurrency"):
        cost = num(r.get(k))
        if cost:
            break
    if not cost:
        continue
    res = str(first(r, "instanceName", "resourceName", "instanceId", "resourceId")).rstrip("/").rsplit("/", 1)[-1] or "(sem nome)"
    cat = first(m, "meterCategory") or first(r, "meterCategory", "consumedService")
    name = first(m, "meterName") or first(r, "meterName", "product")
    tot[f"{res} — {cat} / {name}"] += cost
    cur = first(r, "currency", "billingCurrency", "billingCurrencyCode") or cur
if not tot:
    print("  (sem valores aqui: veja em Gerenciamento de Custos > Análise de custo, agrupado por Recurso)")
for k, v in tot.most_common(12):
    print(f"  {v:8.2f} {cur}  {k}")
'

if [ "${1:-}" != "reduzir" ]; then
  say "Nada foi alterado. Se o compartilhamento acima estiver acima de 32 GiB / 500 IOPS / 60 MiB/s, rode: bash custo-armazenamento.sh reduzir"
  exit 0
fi

say "Reduzindo $SHARE ao mínimo (32 GiB, 500 IOPS, 60 MiB/s em HDD provisionado v2)"
if az storage share-rm update -g "$RG" --storage-account "$SA" -n "$SHARE" --quota 32 \
     --provisioned-iops 500 --provisioned-bandwidth-mibps 60 -o none 2>/tmp/reduzir.err; then
  echo "  pronto: o novo valor vale a partir de agora (a Azure só deixa reduzir de novo depois de 24 h)."
else
  echo "  A Azure recusou:"; sed 's/^/    /' /tmp/reduzir.err | head -5
  cat <<'EOF'
  Se for camada Premium (mínimo 100 GiB pago) ou a Azure não deixar reduzir: no portal, abra a conta de
  armazenamento > Compartilhamentos de arquivos > byte-dados > "Alterar tamanho e desempenho" e ponha o mínimo.
  Se ainda assim ficar caro, me avise: dá para trocar o compartilhamento por um de "pagamento conforme o uso"
  (poucos centavos por mês para alguns MB) ou levar a cópia dos dados para o seu Google Drive.
EOF
fi
