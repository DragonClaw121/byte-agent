# Rotina (v13.29): o agente aprende seus horários e lugares e sugere no momento certo

Com o tempo, o Kronus (e qualquer agente) aprende a sua rotina **sozinho**, sem a LLM e sem gastar tokens. Ele usa
três coisas:

| O que ele observa | De onde vem | O que aprende |
|---|---|---|
| quando você fala com ele | conversas (Telegram, app, painel) | seu horário ativo em cada dia da semana |
| por onde você passa | localização do app (📍, opcional) | casa, trabalho e a hora em que você costuma sair |
| o que você pede toda semana | seus lembretes | repetições que podem virar lembrete fixo |

Ele **não guarda o seu trajeto**: só contagens por área de ~1 km e por hora da semana. As contagens perdem metade do peso
a cada ~3 semanas, então, se a rotina muda, as sugestões mudam junto.

## O que ele sugere
- ☔ **Chuva na saída:** cerca de 45 min antes da hora em que você costuma sair, se houver 50% ou mais de chance de
  chuva: "leve o guarda-chuva".
- 🚗 **Hora de sair:** para compromissos da agenda que têm endereço, calcula a distância de onde você está e avisa quando
  sair. A estimativa usa 10 min para sair, ~28 km/h na cidade e ~70 km/h na estrada. No app do Kronus, esse aviso pode
  tocar como ligação, na opção "Compromissos chegando e hora de sair".
- 🔁 **Repetição:** "nas últimas 3 semanas, às segundas por volta das 9h, você me pediu 'pagar a academia'. Quer um
  lembrete fixo?". Responda `/rotina aceitar <n>`.
- ⏰ **Horário do resumo:** se você costuma começar o dia bem depois do resumo da manhã, ele sugere mudar o horário.
- 💡 **No resumo da manhã:** a chuva perto da sua saída e o maior horário livre da agenda para a tarefa mais importante.

Limites: no máximo 4 sugestões por dia e nenhuma fora do seu horário ativo. A chuva na saída e o "hora de sair" valem
sempre, porque estão presos a um horário seu.

## Comandos
- `/rotina`: mostra o que ele aprendeu (casa, horários de saída, horário ativo, sugestões abertas).
- `/rotina aceitar 1`: transforma uma repetição em lembrete semanal.
- `/rotina off` ou `/rotina on`: para ou volta a aprender e sugerir.
- `/rotina esquecer`: apaga tudo o que ele aprendeu.
- `/rotina 08:00 <instrução>`: continua agendando uma tarefa diária, como antes.

## Para funcionar melhor
1. No app do Kronus, ligue **📍 Compartilhar minha localização** e **📞 Kronus pode me ligar**. O serviço atento manda a
   posição a cada 15 min.
2. Configure a agenda iCal (`BYTE_AGENDA_ICS`, veja `gatilhos.md`) e ponha **endereço** nos compromissos.
3. Dê uns dias: casa e saídas aparecem depois de algumas noites e de ~3 semanas do mesmo padrão.
