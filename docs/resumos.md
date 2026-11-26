# Resumo da manhã e fechamento da semana (v13.24)

O agente manda sozinho, no horário que você escolher:

**☀️ Resumo da manhã**, todo dia (ou só de segunda a sexta), com:
- 🌤️ o clima da sua cidade (opcional, Open-Meteo, sem chave);
- 📅 os compromissos de hoje (agenda do Google/Outlook pelo endereço iCal, segredo `BYTE_AGENDA_ICS`);
- ⏰ os lembretes de hoje;
- 📌 as tarefas pendentes mais importantes;
- 🧩 os objetivos em andamento (e os travados);
- 📬 os e-mails não lidos, só remetente e assunto, se o agente tiver e-mail (o Bit);
- 📰 as manchetes dos temas que você escolher (opcional; notícias das últimas 48 h pelo Tavily).

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

## Ligar o clima e as notícias
Os dois vêm **desligados** até você escolher a cidade e os temas. Faça isso em cada agente (cada um tem as suas
configurações), no Telegram dele:
```
/resumo cidade São Paulo
/resumo noticias inteligência artificial, games, economia
/resumo
```
O último comando gera o resumo na hora, para conferir. No painel, preencha os campos na aba **Agenda** → **☀️ Resumos**
e clique em **Salvar horários**.

- **Clima:** usa a Open-Meteo, gratuita e sem chave. Só o nome da cidade sai do servidor. Se o nome não for
  encontrado, o resumo avisa. Use o nome simples da cidade, sem estado.
- **Notícias:** usam o **Tavily** (segredo `TAVILY_API_KEY`), no modo notícias. Sem o Tavily, o resumo avisa
  "nenhum buscador de notícias configurado" em vez de mostrar artigos da Wikipédia. O Kronus e o Bit precisam da
  habilidade `web`, e os dois já a têm.
- Para usar a mesma cidade e os mesmos temas em todos os agentes de uma vez, defina as variáveis
  `BYTE_RESUMO_CIDADE` e `BYTE_RESUMO_NOTICIAS` no contêiner. O que for configurado pelo `/resumo` vale mais.
- Algo falhou? `/resumo config` mostra o último problema.

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


## Avisos com retorno e com ação (v13.68)
- **👍 / 👎:** os avisos de **clientes**, **rotina**, **gatilhos**, **iniciativas** (quando o agente puxa assunto) e
  **resumos** chegam com 👍 Ajudou / 👎 Não ajudou, no Telegram, no app e no Bit do PC. No 2º 👎 seguido da mesma
  categoria (em 30 dias), o agente oferece silenciá-la por 30 dias. Lembretes que você pediu, aprovações, atas, saúde
  e atualizações sempre chegam.
- **`/avisos`** mostra as categorias, o placar e o que está silenciado. `/avisos silenciar rotina 15` e
  `/avisos ligar rotina` mudam à mão. Um aviso silenciado não chega em canal nenhum.
- **Botões de ação:** os alertas de renovação e de clientes sem contato trazem "✉️ Renovação — <cliente>" e
  "✉️ Follow-up — <cliente>". Um toque prepara o e-mail no seu modelo, com a ficha do cliente. No Bit do PC, ele abre
  pronto para o Outlook. A ação só prepara: o envio é sempre seu.
- **Para ver agora:** `/alertas_clientes testar` dispara os alertas de clientes na hora, com os botões. Se não houver
  nada para avisar, guarde uma renovação próxima para testar:
  `/data_cliente Teste | renovação Produto X | <data daqui a 10 dias>`.


## Clima (v13.70)
- **Sem cadastro:** o clima do resumo e das perguntas ("vai chover hoje?") usa, nesta ordem:
  1. a cidade que você pedir ("clima em Campinas, SP");
  2. onde você está agora (app do Kronus ou Bit no PC, com a sua permissão; a posição vale para todos os agentes);
  3. a cidade do resumo (`/resumo cidade <nome>`);
  4. a última localização conhecida;
  5. a cidade que você já contou ("moro em Americana").
- **Cidade certa:** a busca é no Brasil (`BYTE_PAIS`, padrão BR), aceita o estado ("Santa Bárbara, MG"). Com várias
  cidades do mesmo nome, prefere a do estado onde você costuma estar e, depois, a maior. O resumo mostra a cidade com o
  estado, para você conferir.
- **Chuva realista:** a chance do dia é a média das horas, não o pico de uma única hora, e só conta como chuva se houver
  chuva prevista em milímetros. Dia seco aparece como "sem chuva"; com chuva, aparecem os milímetros e a chance. O aviso
  "leve o guarda-chuva" da rotina segue a mesma regra.
- **Bit no PC:** a opção **Localização (clima)** usa a localização do Windows (aproximada, pelo Wi-Fi) a cada 45 min. O
  app pergunta uma vez. Se o Windows estiver com a localização desligada (Configurações → Privacidade e segurança →
  Localização), ele avisa.
