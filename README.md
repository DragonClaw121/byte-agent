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

Atualizado na v13.68. As notas das versões aparecem no próprio aviso de atualização (`byte_agent/novidades.json`).

| Área | Situação |
|---|---|
| Projeto versionado com testes e CI (Linux, Windows real, Android) | feito |
| Nuvem (Azure Container Apps): um processo por agente, cópias diárias, atualização só com testes verdes | feito |
| Canais: Telegram, painel web, API, app Android (Kronus e convidados), Bit no PC (Windows) | feito |
| Lembretes, rotinas, gatilhos, resumos da manhã e da semana, rotina aprendida | feito |
| Agenda e arquivos: Google e Microsoft (Outlook, OneDrive, Teams), iCal | feito |
| E-mail: várias caixas, triagem, modelos com o formato do Outlook, abrir no Outlook clássico e novo | feito |
| Pesquisa na web, pesquisa de cliente (CNPJ + plano de conta) e ficha do cliente com datas e alertas | feito |
| Reuniões: transcrição ao vivo (celular e PC) e ata com tarefas, ligada à ficha do cliente | feito |
| Permissões por tipo de ação, aprovações persistentes, avaliações, falhas visíveis no `/saude` | feito |
| Vários usuários (convidados com espaço próprio, e-mail próprio e app genérico) | feito |
| Avisos com botões de ação e 👍/👎 (silenciar o que não ajuda) | feito (v13.68) |
| Microsoft Graph com a conta da empresa (rascunho direto no Outlook) | aguarda a aprovação da TI |
| Live da Byte (Twitch + VTube Studio + voz) | código pronto e testado só com simulações; falta o "Estúdio" no PC (a nuvem não alcança o VTube Studio) |
| Fala da VTuber enquanto gera (TTS por frase) | pendente |
| Objetivos de trabalho recorrentes e autonomia graduada (escada de confiança) | pendente |
| Sandbox real para o código aprovado (contêiner separado, sem o disco de dados) | pendente (hoje: subprocesso sem segredos no ambiente) |
| Empresas: instância por empresa, administração, LGPD, login corporativo | futuro (quando houver clientes) |

**Sobre dividir `engine.py` em módulos:** o painel (HTML/JS) já saiu para `byte_agent/painel/painel.html` (v13.61);
o resto continua no motor de propósito. O `MetacognitiveSelfEvolver` troca funções
em tempo de execução pelo dicionário global do módulo; uma divisão mecânica quebraria esse recurso em silêncio.
A ordem segura é: extrair o HTML/JS do painel para arquivos próprios, depois `VectorEngine`/`HybridContextReader`,
depois o roteador, rodando `pytest` a cada extração.

- **Objetivos (v13.9):** tarefas longas que a Byte executa sozinha, em etapas — [docs/objetivos.md](docs/objetivos.md)
- **Avaliações de comportamento (v13.10):** nota por categoria e regressões — [docs/avaliacoes.md](docs/avaliacoes.md)
- **Atualização automática da nuvem pelo GitHub (v13.10):** [docs/azure-conteiner.md](docs/azure-conteiner.md)
- **Gatilhos (v13.14):** avisos por evento — agenda, e-mail novo, tarefa parada — [docs/gatilhos.md](docs/gatilhos.md)
- **App Kronus para Android (nativo, microfone do relógio):** [docs/app-android.md](docs/app-android.md)
- **Permissões (v13.23):** sugerir, pedir aprovação ou fazer e registrar, por tipo de ação — [docs/permissoes.md](docs/permissoes.md)
- **Resumos (v13.24):** resumo da manhã e fechamento da semana, com horário por agente — [docs/resumos.md](docs/resumos.md)
- **Planilhas e documentos (v13.26):** o Bit cria .xlsx com fórmulas e gráficos e .docx, também a partir dos seus modelos — [docs/documentos.md](docs/documentos.md)
- **E-mail completo (v13.27):** várias caixas, de qualquer domínio (Gmail, Outlook/Microsoft 365, e-mail da empresa) — [docs/email.md](docs/email.md)
- **Clima, localização e notícias (v13.28):** todo agente consulta o tempo de verdade; o Kronus segue onde você está (app 1.13) — `/clima`, `/onde`
- **Rotina (v13.29):** aprende casa, saídas e horários; avisa chuva na saída, hora de sair e repetições — [docs/rotina.md](docs/rotina.md)
- **Base sólida (R0, v13.31–13.34):** aprovações no banco por 24 h, um processo por agente, memória por significado (Azure), relógio central com `/saude` e aviso quando algo falha — [docs/azure-conteiner.md](docs/azure-conteiner.md)
- **Reuniões (v13.36–13.37):** transcrição ao vivo pelo app ou pelo notebook (Teams/Zoom/Meet) e ata com tarefas — [docs/app-android.md](docs/app-android.md)
- **Microsoft (v13.38):** agenda do Outlook unificada, criar/mover/cancelar com aprovação, OneDrive automático e transcrições do Teams — [docs/microsoft.md](docs/microsoft.md)
- **Google (v13.39):** Google Agenda na agenda unificada (com Meet) e Drive automático — [docs/google.md](docs/google.md)
- **Pesquisa de cliente (v13.50):** cadastro oficial pelo CNPJ, web só sobre o cliente e plano de abordagem com os seus dados de compra (.docx + e-mail) — `/cliente`, ou "pesquise esse cliente" no Bit do PC — [docs/clientes.md](docs/clientes.md)
- **Bit no PC (v13.47):** app para Windows — conversa, "olha minha tela" e ajuda ao vivo vendo a tela, passo a passo, com marcação de onde clicar — [docs/bit-pc.md](docs/bit-pc.md)
- **Pessoas (v13.42):** convide outras pessoas pelo mesmo bot (link `t.me/...`) ou por um painel pessoal (`/u/<agente>`); cada uma com conversa, memória, tarefas e lembretes próprios e limite diário — [docs/pessoas.md](docs/pessoas.md)
