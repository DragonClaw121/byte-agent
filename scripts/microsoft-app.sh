#!/usr/bin/env bash
# Prepara a parte Microsoft dos agentes (agenda do Outlook, OneDrive, transcrições do Teams e e-mail Outlook/Hotmail):
# registra o aplicativo "Byte Email" no SEU Azure (gratuito) e grava no contêiner:
#   BYTE_MS_CLIENT_ID   o id do aplicativo (não é segredo)
#   BYTE_CHAVE_TOKENS   a chave que criptografa os logins da Microsoft (segredo "chave-tokens", gerado aqui)
# Rode no Cloud Shell (bash), uma vez:
#
#   bash microsoft-app.sh
#
# Depois, no Telegram do agente: /microsoft entrar  (e, para o Teams da empresa, /microsoft entrar teams)
set -euo pipefail
RG="${RG:-byte-rg}"
APP="${APP:-byte}"
say() { printf '\n\033[1m[microsoft] %s\033[0m\n' "$*"; }
az account show -o none 2>/dev/null || { echo "Abra o Cloud Shell (bash) do portal da Azure."; exit 1; }
env_value() {
  az containerapp show -g "$RG" -n "$APP" --query "properties.template.containers[0].env[?name=='$1'].value | [0]" -o tsv 2>/dev/null || true
}
VARS=()

if [ -z "$(az containerapp secret list -g "$RG" -n "$APP" --query "[?name=='chave-tokens'].name" -o tsv 2>/dev/null)" ]; then
  say "Criando a chave que criptografa os logins (fica só nos Segredos do contêiner)"
  az containerapp secret set -g "$RG" -n "$APP" --secrets "chave-tokens=$(openssl rand -base64 32)" -o none
fi
VARS+=("BYTE_CHAVE_TOKENS=secretref:chave-tokens")

CLIENT="$(env_value BYTE_MS_CLIENT_ID)"
if [ -n "$CLIENT" ]; then
  echo "    O aplicativo já existe: $CLIENT"
else
  say "Registrando o aplicativo 'Byte Email' no seu Azure"
  CLIENT="$(az ad app create --display-name "Byte Email" --sign-in-audience AzureADMultipleOrgs \
            --is-fallback-public-client true --query appId -o tsv 2>/dev/null || true)"
  if [ -n "$CLIENT" ]; then
    # contas pessoais (Outlook.com/Hotmail) exigem tokens v2 antes de liberar o público "pessoais + empresas"
    az rest --method PATCH --url "https://graph.microsoft.com/v1.0/applications(appId='$CLIENT')" \
      --headers "Content-Type=application/json" --body '{"api":{"requestedAccessTokenVersion":2}}' -o none 2>/dev/null || true
    az rest --method PATCH --url "https://graph.microsoft.com/v1.0/applications(appId='$CLIENT')" \
      --headers "Content-Type=application/json" --body '{"signInAudience":"AzureADandPersonalMicrosoftAccount"}' -o none 2>/dev/null \
      || echo "    aviso: não consegui liberar contas pessoais (Hotmail/Outlook.com). Veja docs/email.md, parte 'Outlook'."
    echo "    aplicativo criado: $CLIENT (não é segredo)"
  else
    echo "    Não consegui criar pelo terminal. Crie pelo portal (docs/email.md, parte 'Outlook') e cole o"
    read -rp "    ID do aplicativo (cliente): " CLIENT
  fi
fi
[ -n "$CLIENT" ] || { echo "Sem o ID do aplicativo não dá para continuar."; exit 1; }
VARS+=("BYTE_MS_CLIENT_ID=$CLIENT")

say "Aplicando (o contêiner reinicia em ~1 minuto)"
az containerapp update -g "$RG" -n "$APP" --set-env-vars "${VARS[@]}" -o none
say "Pronto. No Telegram do agente (Bit, Matrix ou Kronus):"
echo "    /microsoft entrar          agenda do Outlook e OneDrive"
echo "    /microsoft entrar teams    também as transcrições do Teams (conta de empresa)"
echo "    E-mail Outlook/Hotmail: bash email-conta.sh"
