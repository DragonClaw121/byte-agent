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

say "Custo do armazenamento nos últimos 30 dias, por item (pode demorar; em alguns tipos de assinatura não aparece)"
START="$(date -u -d '30 days ago' +%Y-%m-%d)"; END="$(date -u +%Y-%m-%d)"
az consumption usage list --start-date "$START" --end-date "$END" --include-meter-details -o json 2>/dev/null \
| SA="$SA" python3 -c '
import json, os, sys, collections
try:
    rows = json.load(sys.stdin)
except Exception:
    print("  (a Azure não devolveu o detalhamento: veja em Gerenciamento de Custos > Análise de custo, agrupado por Medidor)")
    sys.exit(0)
sa = os.environ["SA"].lower()
tot = collections.Counter()
cur = ""
for r in rows:
    if sa not in str(r.get("instanceName") or r.get("instanceId") or "").lower():
        continue
    m = r.get("meterDetails") or {}
    key = " / ".join(str(m.get(k) or "") for k in ("meterCategory", "meterSubCategory", "meterName"))
    tot[key] += float(r.get("pretaxCost") or 0)
    cur = r.get("currency") or cur
if not tot:
    print("  (nada encontrado por aqui: veja em Gerenciamento de Custos > Análise de custo, agrupado por Medidor)")
for k, v in tot.most_common(10):
    print(f"  {v:8.2f} {cur}  {k}")
'

if [ "${1:-}" != "reduzir" ]; then
  say "Nada foi alterado. Se aparecer 'provisionado' acima (ou camada Premium), rode: bash custo-armazenamento.sh reduzir"
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
