"""Cliente de referência da API da Byte para dispositivos leves (relógio, celular, atalhos), só biblioteca padrão.

    export KRONUS_API_TOKEN=...          # o mesmo valor do segredo do agente
    python clients/cliente_api.py --url http://127.0.0.1:8772 --token-env KRONUS_API_TOKEN "o que tenho hoje?"
    python clients/cliente_api.py --url ... --token-env ... --notificacoes

Um app de relógio faz exatamente estas três chamadas:
    POST /api/v1/message   {"text": "..."}  -> {"reply", "speech", "pending_approvals"}
    GET  /api/v1/notifications              -> lembretes e avisos ainda não entregues
    GET  /api/v1/status
sempre com o cabeçalho  Authorization: Bearer <token>.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def call(url: str, token: str, method: str = "GET", payload=None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return {"error": f"HTTP {err.code}: {err.read().decode('utf-8', 'replace')[:300]}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Conversa com um agente Byte pela API.")
    parser.add_argument("texto", nargs="?", default="")
    parser.add_argument("--url", required=True, help="ex.: http://127.0.0.1:8772")
    parser.add_argument("--token-env", required=True, help="variável de ambiente com o token (nunca passe o token na linha de comando)")
    parser.add_argument("--notificacoes", action="store_true")
    parser.add_argument("--longo", action="store_true", help="pede resposta completa em vez de curta")
    args = parser.parse_args()
    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"Defina a variável {args.token_env} com o token da API.")
        return 2
    base = args.url.rstrip("/")
    if args.notificacoes:
        res = call(f"{base}/api/v1/notifications", token)
        for item in res.get("notifications", []):
            print(f"• {item['text']}")
        print(res.get("error") or ("(sem notificações)" if not res.get("notifications") else ""))
        return 0
    if not args.texto:
        print(json.dumps(call(f"{base}/api/v1/status", token), ensure_ascii=False, indent=2))
        return 0
    payload = {"text": args.texto}
    if args.longo:
        payload["curto"] = False
    res = call(f"{base}/api/v1/message", token, "POST", payload)
    print(res.get("error") or f"{res['agent']}: {res['reply']}" + (f"\n({res['note']})" if res.get("note") else ""))
    return 1 if res.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
