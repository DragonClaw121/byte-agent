# Integrações de trabalho

Regra geral: **ler é automático, escrever exige aprovação.** Tudo que a Byte cria ou publica em outro sistema aparece
antes num cartão "Aprovar/Descartar" (painel ou Telegram) e só é executado exatamente como foi mostrado. Conteúdo
lido de outro sistema conta como texto de terceiros: depois de lê-lo, a Byte não grava tarefas, memórias ou
lembretes sozinha no mesmo pedido.

As integrações só ligam quando o segredo existe (Colab Secrets ou `.env`). No manifesto do agente, a habilidade é
`integracoes` (ligada por padrão no tipo `trabalho`).

## O que está pronto

| Sistema | Lê (automático) | Escreve (com aprovação) | O que você precisa |
|---|---|---|---|
| **Asana** | suas tarefas abertas, com prazo, projeto e link | criar tarefa atribuída a você | `ASANA_TOKEN`: Asana → My Settings → Apps → Developer apps → Personal access token. `ASANA_WORKSPACE` opcional |
| **Notion** | buscar páginas e ler o texto de uma página | criar página | `NOTION_TOKEN`: notion.so/my-integrations → nova integração interna; depois, em cada página, "Conexões" → adicionar a integração. `NOTION_PARENT_PAGE`: id da página onde as novas são criadas |
| **Teams** | — | publicar mensagem num canal | `TEAMS_WEBHOOK_URL`: no canal, Workflows → "Post to a channel when a webhook request is received" → copie a URL |
| **ntfy** (push no celular/relógio) | — | lembretes e avisos chegam como notificação | `BYTE_NTFY_TOPIC`: nome longo e aleatório (16+ caracteres; funciona como senha). Instale o app ntfy e assine o tópico |
| **Documentos** | `.docx`, `.txt`, `.md` (e `.pdf` com `pip install pypdf`) | — | envie para a pasta `arquivos/` ou como documento no Telegram, e use `/ler <arquivo>` |
| **Planilhas** | `.csv`, `.xlsx` | a análise gera também um `.xlsx` com fórmulas (SOMASES etc.) para auditar no Excel | `pip install openpyxl` |

Exemplos de pedido: "o que tenho no Asana esta semana?", "procure a ata de ontem no Notion e resuma",
"crie no Notion uma página com o resumo da reunião", "avise o time no Teams que o relatório saiu".

Rotina diária sugerida (Telegram): `/rotina 08:30 resuma minhas tarefas do Asana, minha agenda e os e-mails não lidos`.

## Viabilidade do que ainda não está pronto

| Integração | Viável? | Como | Bloqueio |
|---|---|---|---|
| E-mail e calendário corporativos (Outlook/Exchange) | Sim | Microsoft Graph com login OAuth da conta da empresa | A TI precisa registrar um aplicativo no Entra ID e liberar as permissões (Mail.Read, Mail.Send, Calendars.Read) |
| Ler conversas do Teams | Sim, com restrição | Microsoft Graph (ChannelMessage.Read) | Permissão de leitura de conversas costuma exigir aprovação de administrador |
| Gmail e Google Agenda | Sim | OAuth do Google | Criar projeto no Google Cloud e tela de consentimento; contas Workspace podem exigir liberação da TI |
| Trello, ClickUp, Jira, Monday | Sim | Mesmo padrão do Asana (token pessoal + leitura automática + escrita com aprovação) | Nenhum técnico: cada uma é um conector pequeno |
| WhatsApp | Parcial | WhatsApp Business API (Meta) | Conta comercial verificada; não serve para o WhatsApp pessoal |
| Slack | Sim | App do Slack com token de bot | Instalar o app no workspace (pode exigir admin) |
| Planilhas do Google | Sim | API do Google Sheets com OAuth | Mesmo projeto do Google acima |

O que destrava a maior parte da rotina corporativa é o **Microsoft Graph** (e-mail, agenda, Teams, arquivos do
OneDrive/SharePoint). Peça à TI um "app registration" com permissões delegadas e eu implemento o conector.
