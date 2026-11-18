# Byte — agente pessoal

Núcleo de agentes com LLMs locais (Ollama). Sem manifesto, quem atende é a **Matrix**, o perfil padrão; Byte (VTuber),
Bit (trabalho) e Kronus (relógio/celular) são agentes com personalidade própria ([docs/agentes.md](docs/agentes.md)).
Memória, agenda, e-mail com aprovação humana, painel web e ponte para Telegram.
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
  work.py              perfil de trabalho: triagem de e-mails, análises e apresentações
  work_data.py         leitura de CSV/XLSX, estatística, relatório HTML, apresentação HTML/PPTX
  web.py               busca, leitura segura de páginas e pesquisa profunda com fontes
  agents.py            núcleo replicável: manifestos de agentes (identidade, habilidades, canais)
  agentes/             agentes de exemplo: byte (VTuber), bit (trabalho), kronus (relógio/celular)
  integrations.py      Asana, Notion, Teams e push pelo ntfy (escrita sempre com aprovação)
  evaluate.py          placar comparando modelos em casos fixos
agents/                seus manifestos (têm prioridade sobre os exemplos)
clients/               cliente de referência da API para dispositivos leves
tests/                 pytest (Ollama, nvidia-smi, ollama.com e Telegram simulados)
deploy/                Dockerfile, docker-compose.yml e unidade systemd
```

## 1. Rodar no seu computador

**Windows + WSL, do zero (instalação, Tailscale, agentes e atualizações):** [docs/guia-completo.md](docs/guia-completo.md).
**Online 24/7 na Azure (servidor pequeno + Azure OpenAI):** [docs/azure.md](docs/azure.md).
**Online 24/7 na Azure sem VM (Container Apps):** [docs/azure-conteiner.md](docs/azure-conteiner.md).

Pré-requisito: [Ollama](https://ollama.com/download) instalado (no Linux a Byte instala sozinha se faltar).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m byte_agent
```

O terminal mostra o endereço do painel (`http://127.0.0.1:8765/ui?token=...`). Os dados ficam em `~/.byte-agent`.

No Linux/WSL, `scripts/byte.sh` resume a rotina: `bash scripts/byte.sh iniciar | parar | atualizar | backup | status`
(segredos em `~/.byte-env`, fora do repositório).

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

**Telegram no Colab:** crie os segredos `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID` (ícone de chave, com acesso do
notebook liberado). O `iniciar_byte()` liga a ponte sozinho. Só com o token, o bot sobe em modo privado e informa
o seu `chat_id` quando você escrever para ele. Para não ligar: `iniciar_byte(telegram=False)`.

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

## Perfil de trabalho

Instância separada, com visual limpo, para e-mails, análises de dados e apresentações. Nenhum e-mail sai sem
aprovação e todo número de análise é calculado por código. Guia completo: [docs/perfil-trabalho.md](docs/perfil-trabalho.md).

```bash
python -m byte_agent --profile trabalho --data-dir ~/byte-trabalho --port 8766
```

Com uma única instância, o seletor **Matrix · Trabalho** no título do painel (ou `/perfil` no Telegram) troca de
perfil sem reiniciar nada; cada perfil mantém seus próprios dados.

## Vários agentes, o mesmo núcleo

Cada agente é um manifesto JSON em `agents/`: nome, personalidade, dono, habilidades e canais (painel, Telegram, API
para relógio/celular, Twitch, legenda do OBS). Guia completo: [docs/agentes.md](docs/agentes.md).

```bash
python -m byte_agent --agent kronus
```

Integrações de trabalho (Asana, Notion, Teams, push no celular, documentos e planilhas):
[docs/integracoes.md](docs/integracoes.md).

## Comando de voz

Todos os agentes atendem por voz: botão 🎙️ no painel, notas de voz no Telegram e a página `/voz` para celular e
relógio (a peça central do Kronus, com modo mãos livres: "Kronus, ..."). A Byte só obedece à voz do dono e, com a
live ligada, responde em voz alta na própria live. Guia: [docs/voz.md](docs/voz.md).

## E-mail com várias contas

Uma conta por provedor (Gmail e Outlook), cada uma com o seu segredo. A Byte procura a senha, nesta ordem: a digitada
na sessão, o segredo que você nomear na aba E-mail, `BYTE_EMAIL_APP_PASSWORD_GMAIL` / `..._OUTLOOK` e, por
compatibilidade, `BYTE_EMAIL_APP_PASSWORD`. Trocar o provedor na aba E-mail troca a conta ativa sem apagar a outra.

## Internet

Busca, leitura de páginas e pesquisa profunda com fontes, só leitura e com proteções contra páginas maliciosas.
Guia: [docs/internet.md](docs/internet.md).

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
| Perfil de trabalho: e-mails, análises, apresentações | feito (testado com e-mail e LLM simulados) |
| Pesquisa na web (busca, leitura, pesquisa profunda) | feito, só leitura ([docs/internet.md](docs/internet.md)) |
| Microsoft Graph (e-mail corporativo com login da empresa) | pendente: depende de app registrado pela TI |
| Vários usuários na mesma Byte | pendente |
| Núcleo replicável (manifestos de agentes) | feito; um agente por processo |
| API para relógio/celular | feito (pull de notificações; push pendente) |
| Legenda da VTuber no OBS | feito |
| Asana, Notion, Teams (escrita com aprovação), push via ntfy | feito |
| Planilha XLSX com fórmulas, leitura de DOCX/PDF | feito |
| Avaliação de modelos (`avaliar`) | feito |
| Troca de agentes pelo painel e pelo Telegram (`/agente`) | feito |
| Comando de voz (painel, Telegram, página /voz, live da Byte) | feito (transcrição e fala dependem de serviço configurado) |
| Vários agentes num único processo | pendente |
| Fala da VTuber enquanto gera (TTS por frase) | pendente |
| 6. Sandbox de execução e permissões por ferramenta | pendente |
| 7. Cliente leve em cada máquina | pendente |

**Sobre dividir `engine.py` em módulos:** ainda não foi feito de propósito. O `MetacognitiveSelfEvolver` troca funções
em tempo de execução pelo dicionário global do módulo; uma divisão mecânica quebraria esse recurso em silêncio.
A ordem segura é: extrair o HTML/JS do painel para arquivos próprios, depois `VectorEngine`/`HybridContextReader`,
depois o roteador, rodando `pytest` a cada extração.

- **Objetivos (v13.9):** tarefas longas que a Byte executa sozinha, em etapas — [docs/objetivos.md](docs/objetivos.md)
- **Avaliações de comportamento (v13.10):** nota por categoria e regressões — [docs/avaliacoes.md](docs/avaliacoes.md)
- **Atualização automática da nuvem pelo GitHub (v13.10):** [docs/azure-conteiner.md](docs/azure-conteiner.md)
- **Gatilhos (v13.14):** avisos por evento — agenda, e-mail novo, tarefa parada — [docs/gatilhos.md](docs/gatilhos.md)
