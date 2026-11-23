# Microsoft: agenda do Outlook, OneDrive e transcrições do Teams (v13.38)

Funciona com conta **pessoal** (Outlook.com, Hotmail) e de **empresa** (Microsoft 365).

## Conectar
1. É preciso o aplicativo "Byte Email" no seu Azure (`BYTE_MS_CLIENT_ID`) e a chave que criptografa os logins. No
   Cloud Shell, baixe o script:
   ```
   curl -sLO https://raw.githubusercontent.com/DragonClaw121/byte-agent/main/scripts/microsoft-app.sh
   ```
   E rode uma vez:
   ```
   bash microsoft-app.sh
   ```
   O `email-conta.sh` faz a mesma coisa quando você adiciona uma conta Outlook ou Hotmail.
2. No Telegram do agente (ou no app, em Comandos → 🔑 Conectar a Microsoft):
   ```
   /microsoft entrar
   ```
   Abra o link, digite o código e aceite **agenda e arquivos**. O agente avisa quando terminar.
3. Para as **transcrições do Teams** (só conta de empresa):
   ```
   /microsoft entrar teams
   ```
   Se aparecer "precisa de aprovação do administrador", a TI da empresa precisa liberar o aplicativo.

O login fica guardado **criptografado** (chave `chave-tokens`) e se renova sozinho. `/microsoft sair` desconecta.

## Agenda do Outlook
- Entra na **agenda unificada**, junto com o link iCal e, depois, o Google. Ela é usada pelo resumo da manhã, pelo aviso
  "📅 Em 30 min...", pelo "hora de sair" da rotina e pela busca de horários livres. Um compromisso que aparece nas duas
  fontes é mostrado uma vez só.
- `/compromissos`, `/compromissos amanha` ou `/compromissos semana`. Na conversa: "o que eu tenho amanhã?".
- **Criar, mudar e cancelar**, por exemplo "marca uma call com cliente@x.com quinta às 14h30, com link do Teams". Isso
  **sempre pede aprovação**, porque um convite sai para outras pessoas: o tipo "calendario" em `/permissoes` tem
  máximo "aprovar".
- Reuniões online não geram "hora de sair".

## OneDrive
- **Atas, planilhas, documentos e apresentações** vão sozinhos para a pasta **Byte** do seu OneDrive.
  `/microsoft onedrive off` desliga.
- Na conversa: "salva a planilha de gastos no OneDrive".

## Transcrições oficiais do Teams
Com o login "teams", a cada 15 minutos o agente procura as reuniões **que você organizou** e que têm transcrição (ligue
**Iniciar transcrição** na reunião). Cada transcrição nova vira **ata automaticamente**, já com o **nome de quem
falou**, e chega no Telegram e no app com o .docx e o botão para criar as tarefas.

Reuniões que você não organizou, ou sem transcrição ligada: use a página `/reuniao` no notebook (veja
`docs/app-android.md`).
