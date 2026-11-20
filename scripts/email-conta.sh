#!/usr/bin/env bash
# Adiciona uma caixa de e-mail ao Bit na nuvem: Gmail, Outlook/Hotmail, Microsoft 365, Yahoo, iCloud ou o e-mail da sua
# empresa (qualquer domínio). Rode no Cloud Shell (bash), uma vez por conta:
#
#   bash email-conta.sh
#
# A senha é pedida sem aparecer na tela e vai direto para os Segredos do contêiner (não fica em arquivo nem no histórico).
# Contas Microsoft não usam senha: depois, no Telegram do Bit, mande /email entrar <id>.
set -euo pipefail

RG="${RG:-byte-rg}"
APP="${APP:-byte}"
say() { printf '\n\033[1m[bit] %s\033[0m\n' "$*"; }
az account show -o none 2>/dev/null || { echo "Abra o Cloud Shell (bash) do portal da Azure."; exit 1; }

env_value() {      # valor atual de uma variável do contêiner (vazio se não existir)
  az containerapp show -g "$RG" -n "$APP" --query "properties.template.containers[0].env[?name=='$1'].value | [0]" -o tsv 2>/dev/null || true
}
has_secret() {
  [ -n "$(az containerapp secret list -g "$RG" -n "$APP" --query "[?name=='$1'].name" -o tsv 2>/dev/null)" ]
}

read -rp "    Endereço de e-mail: " ADDR
ADDR="$(printf '%s' "$ADDR" | tr -d '[:space:]')"
[[ "$ADDR" =~ ^[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$ ]] || { echo "Endereço inválido."; exit 1; }
DOMAIN="$(printf '%s' "${ADDR#*@}" | tr '[:upper:]' '[:lower:]')"

KIND=senha
case "$DOMAIN" in
  outlook.com|outlook.com.br|hotmail.com|hotmail.com.br|live.com|live.com.br|msn.com) KIND=microsoft ;;
  gmail.com|googlemail.com|yahoo.com|yahoo.com.br|ymail.com|icloud.com|me.com|mac.com|uol.com.br|bol.com.br|terra.com.br) KIND=senha ;;
  *)
    MX="$(curl -s "https://dns.google/resolve?name=$DOMAIN&type=MX" | tr ',' '\n' | grep -o '"data":"[^"]*' | head -1 || true)"
    case "$MX" in *outlook.com*) KIND=microsoft; echo "    $DOMAIN usa o Microsoft 365 (login pela Microsoft)." ;; esac ;;
esac

BASE="$(printf '%s' "${DOMAIN%%.*}" | tr -cd 'a-z0-9')"
case "$BASE" in googlemail) BASE=gmail ;; live|msn) BASE=outlook ;; esac
read -rp "    Apelido da conta [$BASE] (letras e números; ex.: pessoal, trabalho): " ID
ID="$(printf '%s' "${ID:-$BASE}" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')"
[[ "$ID" =~ ^[a-z][a-z0-9]{0,23}$ ]] || { echo "Apelido inválido: comece com letra."; exit 1; }
UPPER="$(printf '%s' "$ID" | tr '[:lower:]' '[:upper:]')"

CONTAS="$(env_value BYTE_EMAIL_CONTAS)"
if printf '%s' "$CONTAS" | grep -qi -- "$ADDR"; then
  echo "    $ADDR já está na lista; só atualizo a senha/login."
else
  CONTAS="${CONTAS:+$CONTAS, }$ID:$ADDR"
fi
VARS=("BYTE_EMAIL_CONTAS=$CONTAS")

if [ "$KIND" = senha ]; then
  case "$DOMAIN" in
    gmail.com|googlemail.com) echo "    Gmail: use uma SENHA DE APP (myaccount.google.com/apppasswords; precisa da verificação em 2 etapas)." ;;
    yahoo.*|ymail.com) echo "    Yahoo: gere uma senha de app em login.yahoo.com/account/security." ;;
    icloud.com|me.com|mac.com) echo "    iCloud: gere uma senha específica de app em account.apple.com." ;;
    *) echo "    Use a senha da caixa (a mesma do webmail). Se a empresa usa verificação em 2 etapas, uma senha de app." ;;
  esac
  PW=""
  while [ -z "$PW" ]; do
    read -rsp "    Senha (não aparece; cole e tecle Enter): " PW; echo
    PW="${PW//[$'\r\n']/}"
    case "$DOMAIN" in gmail.com|googlemail.com) PW="${PW// /}" ;; esac      # a senha de app do Google vem com espaços
  done
  echo "      ✓ recebida (${#PW} caracteres)"
  SECRET="email-senha-$ID"
  az containerapp secret set -g "$RG" -n "$APP" --secrets "$SECRET=$PW" -o none
  unset PW
  VARS+=("BYTE_EMAIL_SENHA_$UPPER=secretref:$SECRET")
else
  if ! has_secret chave-tokens; then
    say "Criando a chave que criptografa o login da Microsoft (fica só nos Segredos)"
    az containerapp secret set -g "$RG" -n "$APP" --secrets "chave-tokens=$(openssl rand -base64 32)" -o none
  fi
  VARS+=("BYTE_CHAVE_TOKENS=secretref:chave-tokens")
  CLIENT="$(env_value BYTE_MS_CLIENT_ID)"
  if [ -z "$CLIENT" ]; then
    say "Registrando o aplicativo 'Byte Email' no seu Azure (gratuito; é ele que pede o login da Microsoft)"
    CLIENT="$(az ad app create --display-name "Byte Email" --sign-in-audience AzureADMultipleOrgs \
              --is-fallback-public-client true --query appId -o tsv 2>/dev/null || true)"
    if [ -n "$CLIENT" ]; then
      # contas pessoais (Outlook/Hotmail) exigem tokens v2 antes de liberar o público "pessoais + empresas"
      az rest --method PATCH --url "https://graph.microsoft.com/v1.0/applications(appId='$CLIENT')" \
        --headers "Content-Type=application/json" --body '{"api":{"requestedAccessTokenVersion":2}}' -o none 2>/dev/null || true
      az rest --method PATCH --url "https://graph.microsoft.com/v1.0/applications(appId='$CLIENT')" \
        --headers "Content-Type=application/json" --body '{"signInAudience":"AzureADandPersonalMicrosoftAccount"}' -o none 2>/dev/null \
        || echo "    aviso: não consegui liberar contas pessoais (Hotmail/Outlook.com). Veja docs/email.md."
      echo "    aplicativo criado: $CLIENT (não é segredo)"
    else
      echo "    Não consegui criar o aplicativo pelo terminal. Crie pelo portal (docs/email.md, passo 'Outlook') e cole o"
      read -rp "    ID do aplicativo (cliente): " CLIENT
    fi
  fi
  [ -n "$CLIENT" ] && VARS+=("BYTE_MS_CLIENT_ID=$CLIENT")
fi

say "Aplicando (o contêiner reinicia em ~1 minuto)"
az containerapp update -g "$RG" -n "$APP" --set-env-vars "${VARS[@]}" -o none
say "Pronto. Conta '$ID' ($ADDR)."
if [ "$KIND" = microsoft ]; then
  echo "    Agora, no Telegram, com o Bit ativo (/agente bit): /email entrar $ID"
  echo "    Abra o link, digite o código e entre com $ADDR."
else
  echo "    Agora, no Telegram, com o Bit ativo (/agente bit): /email testar $ID"
fi
