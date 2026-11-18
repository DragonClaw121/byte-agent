#!/usr/bin/env bash
# Atalhos para rodar a Byte no Linux/WSL.
#
#   scripts/byte.sh iniciar      liga a instância principal: Matrix, Byte, Bit (painel em http://127.0.0.1:8765)
#   scripts/byte.sh kronus       liga o Kronus dedicado, em paralelo (painel em :8772, página /voz do celular)
#   scripts/byte.sh parar        desliga tudo (ou: parar principal | parar kronus)
#   scripts/byte.sh atualizar    baixa a versão nova do GitHub, reinstala e roda os testes
#   scripts/byte.sh backup       copia os dados (conversas, memórias, agenda) para ~/backups-byte
#   scripts/byte.sh status       mostra Ollama, modelos, Byte e Tailscale
#   scripts/byte.sh instalar-zip ARQUIVO.zip   aplica uma atualização recebida em zip (sem GitHub)
#   scripts/byte.sh logs [kronus]  (servidor) acompanha o registro do serviço
# No servidor (scripts/instalar-servidor.sh), iniciar/kronus/parar controlam os serviços do systemd.
#
# Segredos ficam em ~/.byte-env (fora do repositório). Variáveis opcionais:
#   BYTE_MODEL=qwen2.5:3b   modelo inicial        BYTE_AUTONOMIA=1   liga as iniciativas espontâneas
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${BYTE_ENV_FILE:-$HOME/.byte-env}"
cmd="${1:-iniciar}"
[ $# -gt 0 ] && shift

cd "$DIR"
if [ ! -d .venv ]; then
  echo "[byte] Criando o ambiente Python (.venv)..."
  python3 -m venv .venv
  .venv/bin/pip install -q -e ".[trabalho,dev]"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
else
  echo "[byte] Aviso: $ENV_FILE não existe; Telegram, voz e Kronus no celular ficam desligados."
fi
MODEL="${BYTE_MODEL:-qwen2.5:3b}"
MAIN_PAT="python -m byte_agent --model"            # instância principal (sem --agent)
KRONUS_PAT="python -m byte_agent --agent kronus"   # Kronus dedicado
ANY_PAT="python -m byte_agent"
extra=(--no-autonomy)
[ "${BYTE_AUTONOMIA:-0}" = "1" ] && extra=()
# servidor com serviços instalados (e este comando NÃO está sendo rodado pelo próprio serviço)
SERVICOS=0
[ -f /etc/systemd/system/byte.service ] && [ "${BYTE_SERVICO:-0}" != "1" ] && SERVICOS=1

if [ "$SERVICOS" = "1" ]; then
  case "$cmd" in
    iniciar) sudo systemctl start byte && echo "[byte] Serviço principal ligado (journalctl -u byte -f)"; exit 0 ;;
    kronus)  sudo systemctl start byte-kronus && echo "[byte] Kronus ligado (journalctl -u byte-kronus -f)"; exit 0 ;;
    parar)
      case "${1:-tudo}" in
        principal) units=(byte) ;; kronus) units=(byte-kronus) ;; tudo) units=(byte byte-kronus) ;;
        *) echo "[byte] Use: parar | parar principal | parar kronus"; exit 1 ;;
      esac
      sudo systemctl stop "${units[@]}" && echo "[byte] Desligado: ${units[*]}"; exit 0 ;;
    reiniciar) sudo systemctl restart byte byte-kronus && echo "[byte] Reiniciados."; exit 0 ;;
  esac
fi

case "$cmd" in
  logs)
    unit="byte"; [ "${1:-}" = "kronus" ] && unit="byte-kronus"
    exec journalctl -u "$unit" -f -n 50
    ;;
  iniciar)
    if pgrep -f "$MAIN_PAT" >/dev/null; then
      echo "[byte] A instância principal já está rodando. Use: scripts/byte.sh parar principal"
      exit 1
    fi
    exec python -m byte_agent --model "$MODEL" "${extra[@]}" "$@"
    ;;
  kronus)
    if pgrep -f "$KRONUS_PAT" >/dev/null; then
      echo "[byte] O Kronus dedicado já está rodando. Use: scripts/byte.sh parar kronus"
      exit 1
    fi
    [ -n "${KRONUS_API_TOKEN:-}" ] || echo "[byte] Aviso: KRONUS_API_TOKEN vazio em $ENV_FILE: a página /voz do celular não vai funcionar."
    exec python -m byte_agent --agent kronus --model "$MODEL" "${extra[@]}" "$@"
    ;;
  parar)
    case "${1:-tudo}" in
      principal) pat="$MAIN_PAT" ;;
      kronus)    pat="$KRONUS_PAT" ;;
      tudo)      pat="$ANY_PAT" ;;
      *) echo "[byte] Use: parar | parar principal | parar kronus"; exit 1 ;;
    esac
    if ! pkill -INT -f "$pat"; then echo "[byte] Nada rodando."; exit 0; fi
    echo -n "[byte] Desligando"
    for _ in $(seq 1 30); do                      # espera fechar de verdade (o Telegram pode levar alguns segundos)
      pgrep -f "$pat" >/dev/null || { echo " ok."; exit 0; }
      echo -n "."; sleep 1
    done
    pkill -TERM -f "$pat" || true; sleep 2
    pgrep -f "$pat" >/dev/null && { echo " ainda rodando; forçando."; pkill -KILL -f "$pat" || true; }
    echo " ok."
    ;;
  atualizar)
    if pgrep -f "python -m byte_agent" >/dev/null; then
      echo "[byte] Desligue a Byte antes de atualizar (scripts/byte.sh parar)."
      exit 1
    fi
    git pull --ff-only
    pip install -q -e ".[trabalho,dev]"
    python -m pytest -q
    echo "[byte] Atualizada para a versão $(python -c 'from byte_agent import engine; print(engine.BYTE_VERSION)')."
    ;;
  instalar-zip)
    zipfile="${1:-}"
    case "$zipfile" in [A-Za-z]:\\*) command -v wslpath >/dev/null && zipfile="$(wslpath -u "$zipfile")" ;; esac   # C:\... do Windows
    if [ -z "$zipfile" ] || [ ! -f "$zipfile" ]; then
      echo "[byte] Arquivo não encontrado: ${zipfile:-(nenhum)}"
      echo "[byte] Uso: byte instalar-zip /mnt/c/Users/SEU_USUARIO/Downloads/byte-agent-vX.zip"
      found=$(ls -1t /mnt/c/Users/*/Downloads/byte-agent*.zip 2>/dev/null | head -5)
      [ -n "$found" ] && { echo "[byte] Zips encontrados (mais novo primeiro):"; echo "$found" | sed 's/^/    /'; }
      exit 1
    fi
    if pgrep -f "python -m byte_agent" >/dev/null; then
      echo "[byte] Ainda há Byte rodando (inclui o Kronus dedicado). Desligue tudo com: byte parar"
      pgrep -af "python -m byte_agent" | sed 's/^/    /'
      exit 1
    fi
    command -v unzip >/dev/null || { echo "[byte] Instale o unzip: sudo apt install -y unzip"; exit 1; }
    tmp="$(mktemp -d)"
    unzip -q "$zipfile" -d "$tmp"
    [ -d "$tmp/byte-agent/byte_agent" ] || { echo "[byte] Este zip não parece ser do byte-agent."; rm -rf "$tmp"; exit 1; }
    # copia por cima do código; .git, .venv, agents/ (seus manifestos) e dados ficam como estão
    (cd "$tmp/byte-agent" && tar --exclude=./.git --exclude=./.venv --exclude=./agents -cf - .) | tar -xf - -C "$DIR"
    rm -rf "$tmp"
    pip install -q -e ".[trabalho,dev]"
    python -m pytest -q
    echo "[byte] Versão $(python -c 'from byte_agent import engine; print(engine.BYTE_VERSION)') instalada. Ligue com: scripts/byte.sh iniciar"
    ;;
  backup)
    if pgrep -f "python -m byte_agent" >/dev/null; then
      echo "[byte] Desligue a Byte antes do backup (scripts/byte.sh parar): o banco aberto pode sair inconsistente."
      exit 1
    fi
    dest="${1:-$HOME/backups-byte}"
    mkdir -p "$dest"
    file="$dest/byte-$(date +%Y-%m-%d_%H%M).tar.gz"
    dirs=()
    for d in "$HOME/.byte-agent" "$HOME/.byte-agent-trabalho" "$HOME/.byte-agents"; do [ -d "$d" ] && dirs+=("${d#"$HOME"/}"); done
    if [ ${#dirs[@]} -eq 0 ]; then echo "[byte] Nada para copiar ainda."; exit 0; fi
    tar -czf "$file" -C "$HOME" "${dirs[@]}"
    chmod 600 "$file"
    echo "[byte] Backup: $file ($(du -h "$file" | cut -f1))"
    ;;
  status)
    echo "== Ollama";    (ollama list 2>/dev/null || echo "Ollama não respondeu (rode: sudo systemctl start ollama)")
    echo "== Na memória"; (ollama ps 2>/dev/null || true)
    echo "== Principal (Matrix/Byte/Bit, porta 8765)"; (pgrep -af "$MAIN_PAT" >/dev/null && echo "ligada" || echo "desligada")
    echo "== Kronus dedicado (porta 8772)";             (pgrep -af "$KRONUS_PAT" >/dev/null && echo "ligado" || echo "desligado")
    if [ -f /etc/systemd/system/byte.service ]; then
      echo "== Serviços"; for u in byte byte-kronus; do echo "$u: $(systemctl is-active $u 2>/dev/null)"; done
    fi
    echo "== Modelo"; echo "${MODEL}"
    echo "== Tailscale"; (tailscale serve status 2>/dev/null || echo "sem tailscale serve")
    echo "== Disco";     du -sh "$HOME/.byte-agent" "$HOME/.byte-agents" 2>/dev/null || true
    ;;
  *)
    sed -n '2,17p' "$0"
    exit 1
    ;;
esac
