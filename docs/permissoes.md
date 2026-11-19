# Permissões: o que a Byte faz sem você (v13.23)

Cada tipo de ação que **muda** algo tem um nível que você escolhe:

| Nível | O que acontece |
|---|---|
| **só sugerir** | ela sugere ("quer que eu crie a tarefa X?") e não faz nada |
| **pedir aprovação** | aparece um cartão **Aprovar / Descartar** (painel e Telegram); só executa o que foi mostrado |
| **fazer e registrar** | faz sozinha e anota em `/atividade` |

| Tipo | Padrão | Observação |
|---|---|---|
| `lembretes` — lembretes e rotinas | fazer e registrar | |
| `tarefas` — tarefas da agenda | fazer e registrar | vale também para as tarefas que ela extrai da conversa em segundo plano |
| `memorias` — fatos e preferências | fazer e registrar | idem |
| `objetivos` — objetivos de longo prazo | fazer e registrar | |
| `codigo` — rodar código | pedir aprovação | máximo: aprovar |
| `email` — enviar e-mail | pedir aprovação | máximo: aprovar |
| `integracoes` — Asana, Notion, Teams | pedir aprovação | máximo: aprovar |

Leituras (agenda, memórias, busca na web) não mudam nada e não passam por aqui.

**Comandos:** `/permissoes` mostra os níveis; `/permissao tarefas aprovar` muda; `/atividade` lista o que ela fez,
sugeriu ou pediu (também na aba **Agenda** do painel). Em objetivos e rotinas (sem você olhando), "pedir aprovação"
manda o cartão para o Telegram e o painel. As ações que saem para fora e o código não podem ser automáticos nesta
versão: primeiro as avaliações (`/avaliar`, que agora também conferem se ela respeita as permissões) precisam mostrar
que é seguro.
