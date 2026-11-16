# Byte — agente pessoal

Agente pessoal com LLMs locais (Ollama), memória, agenda, e-mail com aprovação humana, painel web e ponte para Telegram.
Só usa a biblioteca padrão do Python (3.10+).

```
byte_agent/
  engine.py            motor completo (o mesmo script único que roda no Colab)
  headless.py          modo servidor: roda fora do Colab, sempre ligada
  telegram_bridge.py   conversa com a Byte pelo Telegram
  twitch_bridge.py     chat da Twitch (IRC sobre TLS, renovação de token)
  vtube_studio.py      API do VTube Studio (expressões e boca) + wsclient.py (WebSocket)
  speech.py            voz por TTS externo com sincronia labial
  stream.py            diretor da live: filtros, persona isolada, emoções
tests/                 pytest (Ollama, nvidia-smi, ollama.com e Telegram simulados)
deploy/                Dockerfile, docker-compose.yml e unidade systemd
```

## 1. Rodar no seu computador

Pré-requisito: [Ollama](https://ollama.com/download) instalado (no Linux a Byte instala sozinha se faltar).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m byte_agent
```

O terminal mostra o endereço do painel (`http://127.0.0.1:8765/ui?token=...`). Os dados ficam em `~/.byte-agent`.

## 2. Falar com a Byte pelo Telegram

1. No Telegram, abra o **@BotFather**, envie `/newbot` e guarde o token.
2. Inicie a Byte só com o token: `BYTE_TELEGRAM_TOKEN=<token> python -m byte_agent`
3. Envie qualquer mensagem ao seu bot. Ele responde com o seu `chat_id` e recusa a conversa.
4. Reinicie com os dois valores:

```bash
BYTE_TELEGRAM_TOKEN=<token> BYTE_TELEGRAM_CHAT_IDS=<seu_chat_id> python -m byte_agent
```

Comandos: `/status`, `/modelo <tag>`, `/agenda`, `/tarefa <texto>`, `/memorias`, `/lembrar <fato>`, `/limpar`,
`/iniciativas on|off`. As iniciativas autônomas chegam como mensagem; e-mails preparados pela LLM chegam com os
botões Aprovar/Descartar. Pelo Telegram nenhum código é executado.

## 3. Deixar sempre ligada (servidor ou VPS com Docker)

```bash
cp .env.example .env        # preencha BYTE_UI_TOKEN, BYTE_TELEGRAM_TOKEN e BYTE_TELEGRAM_CHAT_IDS
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f byte
```

- O painel fica só em `127.0.0.1:8765` do servidor. Para abrir de outro lugar use um túnel:
  `ssh -L 8765:127.0.0.1:8765 usuario@servidor` e acesse `http://127.0.0.1:8765/ui?token=<BYTE_UI_TOKEN>`.
  **Não publique essa porta na internet**: o painel permite executar código na máquina.
- Sem GPU, use modelos pequenos (`qwen2.5:3b`, `llama3.2:3b`) ou configure uma chave de nuvem no `.env`.
- Com GPU NVIDIA, descomente o bloco `deploy:` do serviço `ollama` no compose.
- Sem Docker: veja `deploy/byte.service` (systemd).

## 4. Continuar usando no Colab

Cole o conteúdo de `byte_agent/engine.py` em uma célula e chame `iniciar_byte()`; ou:

```python
!pip install -q git+https://github.com/<seu-usuario>/byte-agent.git
from byte_agent import iniciar_byte
iniciar_byte()
```

## 5. Testes e versionamento

```bash
pytest -q
git remote add origin git@github.com:<seu-usuario>/byte-agent.git
git push -u origin main
```

O GitHub Actions (`.github/workflows/tests.yml`) roda os testes a cada push.

## Ferramentas (modo híbrido)

A cada turno a Byte consulta as capacidades do modelo que vai responder (`/api/show`):

- **Modelo com `tools`** → modo nativo: a LLM chama ferramentas, recebe o resultado e continua (até 5 rodadas).
  Automáticas: `get_datetime`, `list_tasks`, `add_task`, `search_memories`, `add_memory`.
  Com aprovação humana: `run_python`, `run_shell`, `email_inbox`, `email_send`.
- **Modelo sem `tools`, ou se o modo nativo falhar** → modo texto de sempre (`<tool_call>` + contexto injetado no prompt).

O painel mostra em cada resposta qual modo foi usado. `BYTE_NATIVE_TOOLS=0` força o modo texto.

## Autonomia por horário

- **Lembrete:** "me lembra de ligar para o dentista às 15h" (a LLM agenda pela ferramenta), `/lembrete 30m beber água`
  no Telegram, ou o campo de lembretes no painel. No horário, a mensagem chega no Telegram e no painel.
- **Rotina:** `/rotina 08:00 resuma minha agenda de hoje` — todo dia, a Byte executa a instrução sozinha e envia o resultado.
  Tarefas agendadas só usam ferramentas de leitura, agenda e memória: nunca código, e-mail ou novos agendamentos.
- Se a máquina estava desligada na hora, o lembrete é entregue ao ligar, marcado como atrasado; rotinas disparam uma vez.
- `/lembretes` lista, `/cancelar <id>` cancela. Em Docker, defina `TZ` no `.env`.

Execução de código sem aprovação existe (`BYTE_TOOL_AUTORUN=1`), mas não há sandbox: use por sua conta e risco.

## Live na Twitch com VTube Studio

Passo a passo completo em [docs/live-twitch-vtube-studio.md](docs/live-twitch-vtube-studio.md).
Teste rápido do avatar e da voz, sem Twitch: `python -m byte_agent --say "[feliz] Oi, chat!"`

## Estado do roteiro

| Passo | Situação |
|---|---|
| 1. Projeto versionado com testes | feito (o motor ainda é um arquivo único; ver abaixo) |
| 2. Rodar fora do Colab, sempre ligada | feito (`headless.py`, Docker, systemd) |
| 3. Canal em qualquer lugar | feito (Telegram) |
| 4. Chamado de ferramentas nativo do Ollama | feito, híbrido (ver "Ferramentas") |
| 5. Lembretes e rotinas com horário real | feito |
| 5b. Calendário externo, busca e leitura web, arquivos | pendente |
| Live: Twitch + VTube Studio + voz | feito (testado só com servidores simulados) |
| 6. Sandbox de execução e permissões por ferramenta | pendente |
| 7. Cliente leve em cada máquina | pendente |

**Sobre dividir `engine.py` em módulos:** ainda não foi feito de propósito. O `MetacognitiveSelfEvolver` troca funções
em tempo de execução pelo dicionário global do módulo; uma divisão mecânica quebraria esse recurso em silêncio.
A ordem segura é: extrair o HTML/JS do painel para arquivos próprios, depois `VectorEngine`/`HybridContextReader`,
depois o roteador, rodando `pytest` a cada extração.
