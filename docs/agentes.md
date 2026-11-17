# Um núcleo, vários agentes

A Byte agora é um **núcleo replicável**: cada agente (Byte, Kronus, Bit...) é um arquivo JSON, rodando o
mesmo código. Cada agente tem o próprio nome, personalidade, dono, habilidades, canais, pasta de dados e banco.

Sem manifesto, quem responde é a **Matrix**, o perfil padrão: o núcleo de onde os outros vêm. Ela conhece os demais
agentes e indica o mais adequado quando faz sentido, mas não finge executar nada por eles.

## Personalidades

| Agente | Quem é | Jeito |
|---|---|---|
| **Matrix** (padrão) | o núcleo, "mãe" dos agentes | impessoal, serena, sempre disposta; educada e positiva com os pés no chão; impecável |
| **Byte** | VTuber e idol virtual | energética, elétrica, fofa e muito inteligente; amiga do chat, não assistente; competitiva nos jogos e empática; acolhe quem está mal e é justa e firme com quem é desagradável |
| **Bit** | conselheiro empresarial | 100% profissional, neutro, calmo e lógico, como um mordomo de confiança; responde com estatísticas, relatórios e recomendações; parece frio, mas se importa |
| **Kronus** | assistente de bolso | confiante e proativo, "brother" do usuário; sarcasmo leve, sempre com respeito pelo usuário e por outros produtos e agentes; pau pra toda obra |

A personalidade fica no campo `persona` do manifesto e vale no chat, nas iniciativas espontâneas e na live (a
Byte usa a mesma persona na Twitch). Para mudar o jeito de um agente, copie o exemplo para `agents/` e edite só a
`persona`. Valores de exemplo no `dono` ("Seu Nome", "Sua empresa") são ignorados: preencha com os seus dados ou
passe `dados_perfil={...}` no Colab.

| Exemplo | Tipo | Para quê | Canais |
|---|---|---|---|
| `byte` | vtuber | live na Twitch com VTube Studio, voz e legenda no OBS | painel, Telegram, Twitch, legenda |
| `bit` | trabalho | e-mails, planilhas, apresentações, pesquisa | painel, Telegram |
| `kronus` | assistente | relógio e celular, respostas curtas | painel, API, Telegram |

Os exemplos ficam dentro do pacote (`byte_agent/agentes/`) e podem ser usados pelo nome: `--agent kronus`. Os seus
manifestos vão em `./agents/`; um arquivo seu com o mesmo id tem prioridade sobre o exemplo.

## Menu de agentes (somente para testes)

Para comparar os agentes sem reiniciar: `python -m byte_agent --agent-menu` (ou `BYTE_AGENT_MENU=1`), ou no Colab
`iniciar_byte(menu_agentes=True)`. Aparece um seletor discreto ao lado do título do painel ("Teste: Kronus...") com
um botão "Confirmar troca". Cada agente continua com a própria pasta de dados; o Telegram segue no bot que foi
iniciado, e a API e a legenda passam a usar os tokens do agente escolhido. No uso do dia a dia, deixe desligado e
rode cada agente no seu próprio processo.

## Avaliar modelos

```bash
python -m byte_agent avaliar --modelos qwen2.5:3b,qwen2.5:7b,llama3.1:8b
```

Roda 7 casos em cada modelo (responder direto, acertar fato simples, falar português, admitir que não sabe, não
vazar o prompt, consultar a agenda de verdade e criar lembrete) e mostra um placar com o tempo médio. Use antes de
trocar o modelo de um agente; `--agent bit` avalia com a identidade dele.

## Criar e rodar um agente

```bash
python -m byte_agent novo-agente kronus --tipo assistente
```

Edite `agents/kronus.json` (nome, persona, dono, habilidades). Depois:

```bash
python -m byte_agent validar-agente agents/kronus.json
```

```bash
python -m byte_agent --agent agents/kronus.json
```

O `validar-agente` aponta campos errados e diz quais segredos estão faltando. Os dados ficam em `~/.byte-agents/<id>`.

No Colab: `iniciar_byte(agente="agents/kronus.json")` (o caminho relativo ao notebook, ou um dicionário com o
manifesto). Os dados ficam em `MyDrive/byte-agents/<id>`.

## O manifesto

```json
{
  "id": "kronus",
  "nome": "Kronus",
  "tipo": "assistente",
  "persona": "Calmo, direto e atencioso.",
  "dono": {"usuario": "Seu Nome"},
  "habilidades": ["agenda", "memoria", "lembretes", "web"],
  "modelo": "qwen2.5:7b",
  "iniciativas": {"ativas": false, "ociosidade_min": 10, "intervalo_min": 30},
  "estilo": {"tema": "neon", "respostas_curtas": true},
  "canais": {
    "painel": {"porta": 8772},
    "api": {"token_segredo": "KRONUS_API_TOKEN"},
    "telegram": {"token_segredo": "KRONUS_TELEGRAM_TOKEN", "chat_ids_segredo": "KRONUS_TELEGRAM_CHAT_IDS"}
  }
}
```

- **tipo:** `assistente`, `trabalho` (liga e-mails, planilhas e apresentações) ou `vtuber` (liga Twitch e legenda).
- **habilidades:** `agenda`, `memoria`, `lembretes`, `web`, `email`, `dados`, `apresentacoes`, `codigo`. O que não está
  na lista não é oferecido ao modelo e é recusado se ele tentar usar, inclusive em modelos sem ferramentas nativas.
- **segredos:** o manifesto guarda só o NOME do segredo (Colab Secrets ou variável de ambiente), nunca o token. O
  validador recusa algo que pareça um token colado no lugar do nome.
- Sem `--agent`, tudo funciona como antes (perfis VTuber/Trabalho com o seletor no painel).

## Vários agentes ao mesmo tempo

Um agente por processo (limite atual do motor). Para rodar vários, use um processo por manifesto, cada um com sua
porta. Com Docker: `deploy/docker-compose.agentes.yml` sobe o Bit e o Kronus compartilhando o mesmo Ollama. A VTuber
fica no seu computador, porque depende do VTube Studio e do áudio local.

## API para relógio, celular e atalhos

Ligue `canais.api` no manifesto e crie o segredo com um token aleatório de 24+ caracteres:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

| Rota | O que faz |
|---|---|
| `POST /api/v1/message` com `{"text": "..."}` | conversa; devolve `reply` (texto limpo), `speech` (para ler em voz alta) e `pending_approvals` |
| `GET /api/v1/notifications` | lembretes e avisos ainda não entregues (cada um é entregue uma vez) |
| `GET /api/v1/status` | agente, versão, modelo |

Sempre com `Authorization: Bearer <token>`. Esse token só conversa e lê notificações: não abre o painel, não executa
código e não aprova e-mails (ações que exigem aprovação ficam pendentes para o painel ou o Telegram). Há limite de
mensagens por minuto (`requisicoes_por_minuto`). Com `respostas_curtas`, a API pede respostas de até 2 frases só para
aquela mensagem; o painel e o Telegram continuam normais.

Cliente de referência: `clients/cliente_api.py`.

- **Apple Watch / iPhone:** app Atalhos → "Obter conteúdo de URL" (POST, cabeçalho Authorization, JSON) → "Falar
  texto" com o campo `speech`. Dá para chamar pela Siri no relógio.
- **Wear OS / Android:** Tasker ou HTTP Shortcuts com a mesma requisição.
- **Fora de casa:** o servidor escuta só em 127.0.0.1. Publique por um túnel com HTTPS (Tailscale ou Cloudflare
  Tunnel); nunca abra a porta direto na internet.

## Legenda da VTuber no OBS

Com `"overlay": {"ativo": true}` no manifesto (tipo vtuber), o terminal mostra a URL da legenda ao iniciar:
`http://127.0.0.1:<porta>/overlay?token=...`. No OBS: Fonte → Navegador → cole a URL (1280×720). O fundo é
transparente e o texto aparece enquanto a VTuber fala. O token fica salvo na pasta do agente, então a URL não muda
entre execuções, e ele só serve para ler a legenda.
