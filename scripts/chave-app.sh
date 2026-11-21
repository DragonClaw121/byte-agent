#!/usr/bin/env bash
# Cria a chave de assinatura NOVA do app Kronus e guarda nos segredos do GitHub (a antiga ficou pública no repositório).
# Rode no Cloud Shell (bash), uma vez:
#
#   bash chave-app.sh
#
# A chave e a senha não aparecem na tela e não ficam em arquivo: vão direto para os segredos do repositório.
# Depois disso, o próximo app terá outra assinatura: desinstale o Kronus antigo uma vez, instale o novo e pareie de novo.
set -euo pipefail
REPO="${REPO:-DragonClaw121/byte-agent}"
say() { printf '\n\033[1m[kronus] %s\033[0m\n' "$*"; }

command -v gh >/dev/null || { echo "O Cloud Shell precisa do gh (GitHub CLI)."; exit 1; }
if ! gh auth status >/dev/null 2>&1; then
  say "Entre no GitHub (abre um código para você confirmar no navegador)"
  gh auth login --hostname github.com --git-protocol https --web --scopes repo
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
chmod 700 "$TMP"
SENHA="$(openssl rand -base64 30 | tr -d '/+=' | cut -c1-32)"

say "Gerando a chave (RSA 3072, válida por 30 anos)"
openssl req -x509 -newkey rsa:3072 -nodes -days 10950 -subj "/CN=Kronus/O=byte-agent" \
  -keyout "$TMP/chave.pem" -out "$TMP/cert.pem" 2>/dev/null
openssl pkcs12 -export -name kronus -inkey "$TMP/chave.pem" -in "$TMP/cert.pem" \
  -out "$TMP/kronus.p12" -passout "pass:$SENHA" -keypbe AES-256-CBC -certpbe AES-256-CBC -macalg sha256

say "Guardando nos segredos do repositório $REPO"
base64 -w0 "$TMP/kronus.p12" | gh secret set KRONUS_KEYSTORE_B64 --repo "$REPO"
printf '%s' "$SENHA" | gh secret set KRONUS_KEYSTORE_SENHA --repo "$REPO"
unset SENHA
say "Pronto. A chave existe só nos segredos do GitHub."
echo "    Avise o Claude: ele remove a chave antiga do repositório e publica o app com a nova."
echo "    Quando o app novo sair: desinstale o Kronus antigo, instale o novo e pareie com /parear."
