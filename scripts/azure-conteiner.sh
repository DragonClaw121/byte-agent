#!/usr/bin/env bash
# Cria a Byte no Azure Container Apps pelo Cloud Shell (bash), sem depender do portal.
# Ambientes sem destino de logs saem como "Express", que não aceitam armazenamento; aqui o ambiente tem Log Analytics
# e perfis de carga de trabalho.
#
#   bash azure-conteiner.sh
#
# Antes: conta de armazenamento + compartilhamento com o zip da Byte, o conteiner-iniciar.sh e o backup.
# Os segredos são pedidos sem aparecer na tela e não ficam no histórico nem em arquivo.
set -euo pipefail

RG="${RG:-byte-rg}"
LOC="${LOC:-brazilsouth}"
ENV_NAME="${ENV_NAME:-byte-amb}"
APP="${APP:-byte}"
SA="${SA:-bytedadosgabriel}"
SHARE="${SHARE:-byte-dados}"
IMAGE="${IMAGE:-docker.io/library/python:3.12-slim}"
CPU="${CPU:-0.25}"
MEM="${MEM:-0.5Gi}"
MODEL="${MODEL:-azure:byte-mini}"
PAINEL_REMOTO="${PAINEL_REMOTO:-1}"
GITHUB_REPO="${GITHUB_REPO:-DragonClaw121/byte-agent}"   # atualiza sozinho pelo GitHub (vazio = só pelo zip)   # 1 = painéis abrem pelo endereço HTTPS (só com o token)

say() { printf '\n\033[1m[byte] %s\033[0m\n' "$*"; }
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")"; }     # texto entre aspas simples para o YAML

say "Configuração: grupo $RG, região $LOC, ambiente $ENV_NAME, aplicativo $APP, armazenamento $SA/$SHARE"
az account show --query "{assinatura:name}" -o tsv >/dev/null || { echo "Abra o Cloud Shell (bash) do portal da Azure."; exit 1; }

say "1/5 Limpando o que o portal criou como 'Express' (se existir)"
for env in $(az containerapp env list -g "$RG" --query "[].name" -o tsv 2>/dev/null); do
  mode=$(az resource show -g "$RG" -n "$env" --resource-type Microsoft.App/managedEnvironments \
         --api-version 2025-10-02-preview --query "properties.environmentMode" -o tsv 2>/dev/null || true)
  if [ "$mode" = "Express" ]; then
    for app in $(az containerapp list -g "$RG" --query "[?contains(properties.managedEnvironmentId, '/$env')].name" -o tsv); do
      echo "    apagando o aplicativo $app (do ambiente Express $env)"
      az containerapp delete -g "$RG" -n "$app" -y >/dev/null
    done
    echo "    apagando o ambiente Express $env"
    az containerapp env delete -g "$RG" -n "$env" -y >/dev/null
  fi
done

say "2/5 Ambiente (perfis de carga de trabalho + Log Analytics com limite diário)"
# Sem destino de logs a Azure cria o ambiente como "Express", que não aceita armazenamento. Com um Log Analytics ligado
# ela cria o ambiente completo. O espaço de logs fica limitado a 0,1 GB por dia e 30 dias de retenção (centavos/mês).
API=2025-10-02-preview
LAW="${LAW:-byte-logs}"
LAW_ID=""; LAW_KEY=""
prepare_logs() {                 # só é chamado quando o ambiente precisa ser criado
  [ -n "$LAW_ID" ] && return 0
  if ! az monitor log-analytics workspace show -g "$RG" -n "$LAW" >/dev/null 2>&1; then
    echo "    criando o espaço de logs $LAW"
    az monitor log-analytics workspace create -g "$RG" -n "$LAW" -l "$LOC" --retention-time 30 --tags projeto=byte -o none
  fi
  az monitor log-analytics workspace update -g "$RG" -n "$LAW" --quota 0.1 -o none 2>/dev/null \
    || echo "    aviso: não consegui pôr o limite diário de 0,1 GB (o resto segue normal)"
  LAW_ID=$(az monitor log-analytics workspace show -g "$RG" -n "$LAW" --query customerId -o tsv)
  LAW_KEY=$(az monitor log-analytics workspace get-shared-keys -g "$RG" -n "$LAW" --query primarySharedKey -o tsv --only-show-errors)
}
env_mode() {
  az resource show -g "$RG" -n "$1" --resource-type Microsoft.App/managedEnvironments \
     --api-version "$API" --query "properties.environmentMode" -o tsv 2>/dev/null || true
}
if ! az containerapp env show -g "$RG" -n "$ENV_NAME" >/dev/null 2>&1; then
  prepare_logs
  echo "    criando o ambiente $ENV_NAME (leva de 2 a 5 minutos)"
  az containerapp env create -g "$RG" -n "$ENV_NAME" -l "$LOC" --enable-workload-profiles true \
     --logs-destination log-analytics --logs-workspace-id "$LAW_ID" --logs-workspace-key "$LAW_KEY" \
     --tags projeto=byte -o none
fi
mode=$(env_mode "$ENV_NAME")
if [ "$mode" = "Express" ]; then
  # Plano B: pede o modo explicitamente pela API (o nome do valor não é documentado no CLI: tenta os prováveis e
  # mostra a resposta da Azure, que costuma listar os valores aceitos). "" = sem environmentMode, só com os logs.
  echo "    a Azure criou como Express mesmo com logs; apagando e pedindo outro modo explicitamente..."
  az containerapp env delete -g "$RG" -n "$ENV_NAME" -y >/dev/null
  prepare_logs
  SUB=$(az account show --query id -o tsv)
  URL="https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.App/managedEnvironments/$ENV_NAME?api-version=$API"
  LOGS=$(printf '"appLogsConfiguration":{"destination":"log-analytics","logAnalyticsConfiguration":{"customerId":"%s","sharedKey":"%s"}}' "$LAW_ID" "$LAW_KEY")
  ok=""
  for want in "" WorkloadProfiles Standard Default Classic Consumption Dedicated; do
    if [ -n "$want" ]; then MODEF=$(printf '"environmentMode":"%s",' "$want"); else MODEF=""; fi
    body=$(printf '{"location":"%s","tags":{"projeto":"byte"},"properties":{%s%s,"workloadProfiles":[{"name":"Consumption","workloadProfileType":"Consumption"}]}}' "$LOC" "$MODEF" "$LOGS")
    echo "    tentando environmentMode=${want:-(sem informar)}"
    if out=$(az rest --method put --url "$URL" --body "$body" -o none 2>&1); then
      for _ in $(seq 1 60); do                         # a criação é assíncrona: espera terminar
        state=$(az resource show -g "$RG" -n "$ENV_NAME" --resource-type Microsoft.App/managedEnvironments \
                --api-version "$API" --query "properties.provisioningState" -o tsv 2>/dev/null || true)
        { [ "$state" = "Succeeded" ] || [ "$state" = "Failed" ]; } && break
        sleep 10
      done
      mode=$(env_mode "$ENV_NAME")
      echo "      criado: estado ${state:-?}, modo ${mode:-?}"
      if [ "$state" = "Succeeded" ] && [ "$mode" != "Express" ]; then ok=1; break; fi
      az containerapp env delete -g "$RG" -n "$ENV_NAME" -y >/dev/null 2>&1 || true
    else
      [ -n "$LAW_KEY" ] && out=${out//"$LAW_KEY"/***}
      echo "      recusado: $(echo "$out" | tr '\n' ' ' | cut -c1-400)"
    fi
  done
  if [ -z "$ok" ]; then
    echo
    echo "Nenhum modo além de Express foi aceito. Copie TODA a saída acima e mande para o assistente."
    exit 1
  fi
fi
unset LAW_KEY
echo "    ambiente pronto (modo: ${mode:-padrão})"

say "3/5 Ligando o armazenamento $SA/$SHARE ao ambiente"
KEY=$(az storage account keys list -g "$RG" -n "$SA" --query "[0].value" -o tsv --only-show-errors 2>/dev/null)
az containerapp env storage set -g "$RG" -n "$ENV_NAME" --storage-name dados --azure-file-account-name "$SA" \
   --azure-file-account-key "$KEY" --azure-file-share-name "$SHARE" --access-mode ReadWrite -o none
unset KEY

say "4/5 Dados da Byte (as chaves não aparecem enquanto você digita; cole e tecle Enter)"
# Chaves: não aparecem na tela. Depois de colar, o script confirma quantos caracteres recebeu. Campos obrigatórios
# vazios são perguntados de novo (um Enter a mais depois de colar não estraga nada).
ask() {          # ask VAR "pergunta" visível|secreto obrigatório|opcional
  local var=$1 label=$2 kind=$3 need=$4 val=""
  while :; do
    if [ "$kind" = secreto ]; then read -rsp "    $label: " val; echo; else read -rp "    $label: " val; fi
    val="${val//[$'\r\n\t ']/}"                      # tira espaços e quebras de linha que vêm junto ao colar
    if [ -n "$val" ]; then
      [ "$kind" = secreto ] && echo "      ✓ recebido (${#val} caracteres)"
      break
    fi
    if [ "$need" = opcional ]; then echo "      (pulado)"; break; fi
    echo "      vazio — cole de novo (no Cloud Shell: Ctrl+Shift+V ou botão direito → Colar)"
  done
  printf -v "$var" '%s' "$val"
}
echo "    As chaves NÃO aparecem enquanto você cola: cole uma vez e tecle Enter; o script confirma o tamanho."
ask AZ_ENDPOINT "Endpoint da Azure OpenAI (ex.: https://byte-agent-ai.openai.azure.com)" visível obrigatório
ask TG_CHATS    "Seu chat_id do Telegram" visível obrigatório
ask S_AZURE     "Chave da Azure OpenAI" secreto obrigatório
ask S_TG        "Token do bot do Telegram (formato 123456:ABC...)" secreto obrigatório
ask S_GROQ      "Chave do Groq (Enter para pular)" secreto opcional
ask S_TAVILY    "Chave do Tavily (Enter para pular)" secreto opcional
ask S_KRONUS    "KRONUS_API_TOKEN (o mesmo do notebook)" secreto obrigatório
ask S_KTG       "Token do bot do Telegram do KRONUS (Enter para pular)" secreto opcional
ask S_SPEECH    "Chave do Azure AI Speech, para respostas em áudio (Enter para pular)" secreto opcional
case "$S_TG" in *:*) ;; *) echo "      aviso: o token do Telegram costuma ter ':' no meio; confira se colou o certo." ;; esac
OPT_SECRETS=""; OPT_ENV=""                       # Groq e Tavily são opcionais: sem chave, nem cria a variável
if [ -n "$S_GROQ" ]; then
  OPT_SECRETS+=$'\n      - name: groq-key\n        value: '"$(q "$S_GROQ")"
  OPT_ENV+=$'\n          - name: BYTE_STT_URL\n            value: \'https://api.groq.com/openai/v1\''
  OPT_ENV+=$'\n          - name: BYTE_STT_MODEL\n            value: \'whisper-large-v3-turbo\''
  OPT_ENV+=$'\n          - name: BYTE_STT_API_KEY\n            secretRef: groq-key'
fi
if [ -n "$S_KTG" ]; then                          # bot próprio do Kronus (fala com os mesmos chats)
  OPT_SECRETS+=$'\n      - name: kronus-telegram\n        value: '"$(q "$S_KTG")"
  OPT_ENV+=$'\n          - name: KRONUS_TELEGRAM_TOKEN\n            secretRef: kronus-telegram'
  OPT_ENV+=$'\n          - name: KRONUS_TELEGRAM_CHAT_IDS\n            value: '"$(q "$TG_CHATS")"
fi
if [ -n "$S_SPEECH" ]; then                       # voz das respostas (Azure AI Speech na mesma região)
  OPT_SECRETS+=$'\n      - name: speech-key\n        value: '"$(q "$S_SPEECH")"
  OPT_ENV+=$'\n          - name: AZURE_SPEECH_KEY\n            secretRef: speech-key'
  OPT_ENV+=$'\n          - name: AZURE_SPEECH_REGION\n            value: '"$(q "$LOC")"
fi
if [ -n "$S_TAVILY" ]; then
  OPT_SECRETS+=$'\n      - name: tavily-key\n        value: '"$(q "$S_TAVILY")"
  OPT_ENV+=$'\n          - name: TAVILY_API_KEY\n            secretRef: tavily-key'
fi

say "5/5 Criando o aplicativo $APP (1 réplica, $CPU vCPU, $MEM, porta 8772)"
ENV_ID=$(az containerapp env show -g "$RG" -n "$ENV_NAME" --query id -o tsv)
umask 077
YAML=$(mktemp)
trap 'rm -f "$YAML"' EXIT
cat > "$YAML" <<EOF
location: $LOC
type: Microsoft.App/containerApps
tags:
  projeto: byte
properties:
  managedEnvironmentId: $ENV_ID
  workloadProfileName: Consumption
  configuration:
    activeRevisionsMode: Single
    ingress:
      external: true
      targetPort: 8772
      transport: auto
      allowInsecure: false
    secrets:
      - name: azure-key
        value: $(q "$S_AZURE")
      - name: telegram-token
        value: $(q "$S_TG")${OPT_SECRETS}
      - name: kronus-token
        value: $(q "$S_KRONUS")
  template:
    containers:
      - name: byte
        image: $IMAGE
        command: ["/bin/sh"]
        args: ["/mnt/dados/conteiner-iniciar.sh"]
        resources:
          cpu: $CPU
          memory: $MEM
        env:
          - name: BYTE_MODEL
            value: $(q "$MODEL")
          - name: AZURE_OPENAI_ENDPOINT
            value: $(q "$AZ_ENDPOINT")
          - name: AZURE_OPENAI_API_KEY
            secretRef: azure-key
          - name: BYTE_TELEGRAM_TOKEN
            secretRef: telegram-token
          - name: BYTE_TELEGRAM_CHAT_IDS
            value: $(q "$TG_CHATS")${OPT_ENV}
          - name: KRONUS_API_TOKEN
            secretRef: kronus-token
          - name: BYTE_AUTONOMIA
            value: '1'
          - name: BYTE_PAINEL_REMOTO
            value: '$PAINEL_REMOTO'
          - name: BYTE_GITHUB_REPO
            value: '$GITHUB_REPO'
        volumeMounts:
          - volumeName: dados
            mountPath: /mnt/dados
    scale:
      minReplicas: 1
      maxReplicas: 1
    volumes:
      - name: dados
        storageType: AzureFile
        storageName: dados
EOF
unset S_AZURE S_TG S_GROQ S_TAVILY S_KRONUS S_KTG S_SPEECH
if az containerapp show -g "$RG" -n "$APP" >/dev/null 2>&1; then
  az containerapp update -g "$RG" -n "$APP" --yaml "$YAML" -o none
else
  az containerapp create -g "$RG" -n "$APP" --yaml "$YAML" -o none
fi
FQDN=$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)

cat <<EOF

Pronto. Endereço da página de voz do Kronus:  https://$FQDN/voz
Endereços dos painéis (com token):           az containerapp logs show -g $RG -n $APP --tail 300 | grep -o 'Painel [^"]*'
Acompanhar a Byte ligando (Ctrl+C sai):       az containerapp logs show -g $RG -n $APP --follow --tail 50
Reiniciar depois de enviar um zip novo:       az containerapp revision restart -g $RG -n $APP --revision \$(az containerapp revision list -g $RG -n $APP --query "[0].name" -o tsv)
EOF
