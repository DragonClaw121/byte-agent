# Resumo da manhã e fechamento da semana (v13.24)

O agente manda sozinho, no horário que você escolher:

**☀️ Resumo da manhã**, todo dia (ou só de segunda a sexta), com:
- 🌤️ o clima da sua cidade (opcional, Open-Meteo, sem chave);
- 📅 os compromissos de hoje (agenda do Google/Outlook pelo endereço iCal, segredo `BYTE_AGENDA_ICS`);
- ⏰ os lembretes de hoje;
- 📌 as tarefas pendentes mais importantes;
- 🧩 os objetivos em andamento (e os travados);
- 📬 os e-mails não lidos, só remetente e assunto, se o agente tiver e-mail (o Bit);
- 📰 as manchetes dos temas que você escolher (opcional, usa a busca na web).

**📊 Fechamento da semana**, uma vez por semana, com:
- ✅ as tarefas concluídas;
- 🏁 os objetivos concluídos;
- 🤖 o que o agente fez sozinho: lembretes entregues, tarefas anotadas, sugestões e pedidos de aprovação;
- 📌 o que ficou para a próxima;
- 📅 a agenda dos próximos 7 dias.

Os dados são juntados **sem a LLM**: nada é inventado. A LLM escreve só a frase de abertura, com a personalidade do
agente, e respeita o orçamento diário de tokens. Sem LLM, a abertura é fixa. O assunto de um e-mail ou o título de uma
manchete é só mostrado: não vai para a LLM nem vira instrução.

## Cada agente tem os seus horários
| Agente | Manhã | Semana |
|---|---|---|
| Kronus | ligado, 07:00, todos os dias | ligado, domingo 19:00 |
| Bit (e o perfil de trabalho) | ligado, 08:00, segunda a sexta | ligado, sexta 17:30 |
| Matrix, Byte e outros | desligado | desligado |

O Bit roda no mesmo processo que a Matrix e a Byte. Por isso, o resumo dele só chega quando ele é o agente ativo
(`/agente bit`). O Kronus roda à parte e manda sempre.

Se a nuvem estiver reiniciando na hora marcada, o resumo ainda sai em até 3 h (manhã) ou 6 h (semana). Depois disso,
fica para o dia seguinte. Nunca sai duas vezes no mesmo dia ou na mesma semana.

## Comandos (Telegram de cada agente)
- `/resumo`: gera o resumo agora, sem esperar o horário.
- `/resumo config`: mostra os horários.
- `/resumo on` ou `/resumo off`: liga ou desliga o resumo da manhã.
- `/resumo hora 07:30`: muda o horário (e liga).
- `/resumo dias uteis` ou `/resumo dias todos`: segunda a sexta, ou todos os dias.
- `/resumo cidade Curitiba` (ou `/resumo cidade off`): o clima.
- `/resumo noticias IA, games, economia` (ou `/resumo noticias off`): até 3 temas, 2 manchetes de cada.
- `/semana`: gera o fechamento da semana agora.
- `/semana sexta 18:00`: muda o dia e o horário. `/semana on` ou `/semana off` liga ou desliga.

No painel, use a aba **Agenda** → **☀️ Resumos**: horários, cidade, temas e os botões "Ver resumo de hoje" e
"Ver fechamento da semana". Na conversa, pergunte "como está meu dia?" ou "como foi a semana?".

## Tarefas concluídas
Para o fechamento contar o que você fez, marque as tarefas como **concluídas** em vez de apagá-las:
- no painel, o botão **✓** ao lado da tarefa (o **X** continua apagando);
- no Telegram, `/feito 12` ou `/feito conta de luz` (os números aparecem em `/agenda`);
- na conversa: "já paguei a conta de luz". O agente marca sozinho, conforme a permissão de **tarefas** (`/permissoes`).

As tarefas concluídas somem da agenda e são apagadas depois de 90 dias.

## Kronus: resumo como ligação
No app do Kronus (1.12 ou mais novo), em "📞 Kronus pode me ligar", ligue **"Resumo da manhã e fechamento da semana
(lidos em voz)"**. Vem desligado. O resumo toca como chamada recebida e, ao atender, o Kronus lê uma versão falada,
curta e sem listas. A versão escrita continua chegando no Telegram.
