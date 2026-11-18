# Gatilhos: a Byte avisa quando algo acontece (v13.14)

Além de horários marcados (lembretes e rotinas), a Byte agora reage a **eventos**. Nenhum gatilho usa a LLM, então
eles não gastam tokens. Os avisos chegam no painel e no Telegram.

| Gatilho | Quando avisa | Precisa de |
|---|---|---|
| `agenda` | compromisso começando em 30 min | endereço secreto iCal da sua agenda |
| `email` | e-mail novo na caixa de entrada (só remetente e assunto) | e-mail configurado na Byte |
| `tarefa_parada` | tarefa pendente há 5 dias ou mais (lembra de novo a cada 7 dias) | nada |
| `objetivo_travado` | objetivo bloqueado há mais de um dia | nada |

`/gatilhos` mostra o estado; `/gatilho <nome> on|off` liga/desliga (também na aba **Agenda** do painel).

## Ligar a agenda do Google
1. Google Agenda no computador → ⚙️ **Configurações** → na esquerda, clique na sua agenda → role até
   **Integrar agenda** → copie o **Endereço secreto no formato iCal** (termina em `basic.ics`).
   Trate como senha: quem tem esse endereço vê sua agenda. (Outlook: Configurações → Calendário → Calendários
   compartilhados → Publicar um calendário → link ICS.)
2. No Cloud Shell, guarde sem aparecer na tela e ligue:
   ```
   read -rsp "Endereço iCal: " K; echo
   az containerapp secret set -g byte-rg -n byte --secrets agenda-ics="$K" -o none && unset K
   az containerapp update -g byte-rg -n byte --set-env-vars BYTE_AGENDA_ICS=secretref:agenda-ics -o none
   ```

## Ajustes (variáveis)
- `BYTE_GATILHO_AGENDA_MIN=30` — quantos minutos antes avisar.
- `BYTE_GATILHO_TAREFA_DIAS=5` — depois de quantos dias uma tarefa é "parada".
- `BYTE_GATILHO_EMAIL_FILTRO="chefe@empresa.com,urgente"` — só avisa e-mails que tenham um desses termos.
- `BYTE_FUSO=America/Sao_Paulo` — fuso dos horários (a nuvem já usa Brasília por padrão desde a v13.14).

## Segurança
- E-mails: só remetente e assunto aparecem no aviso; o conteúdo é de terceiros e nunca vira instrução. Na primeira
  vez, o que já estava não lido não gera aviso (evita uma enxurrada).
- Agenda: só endereços `https://`; até 5 MB. Eventos recorrentes diários e semanais são entendidos (com exceções).
