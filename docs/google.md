# Google: Agenda e Drive (v13.39)

O agente passa a ver e criar compromissos no **Google Agenda**, com link do Meet e sempre com aprovação, e salva atas,
planilhas e documentos sozinho numa pasta **Byte** do **Google Drive**. Seus e-mails do Gmail continuam pela
senha de app (veja `email.md`).

## 1. Projeto no Google Cloud (uma vez, ~10 minutos)
1. Abra **console.cloud.google.com** com a sua conta Google e crie um projeto (ex.: **Byte**).
2. Menu → **APIs e serviços → Biblioteca**: ative a **Google Calendar API** e a **Google Drive API**.
3. Menu → **Google Auth Platform** (ou "Tela de consentimento OAuth"):
   - **Branding:** nome do app **Byte** e o seu e-mail nos campos de contato.
   - **Público:** **Externo**. Depois clique em **Publicar aplicativo** (status **Em produção**). Em "Teste", o Google
     derruba o login a cada 7 dias. Não precisa pedir verificação: o app é só seu.
4. No **Cloud Shell da Azure**, baixe o script:
   ```
   curl -sLO https://raw.githubusercontent.com/DragonClaw121/byte-agent/main/scripts/google-conta.sh
   ```
   E rode:
   ```
   bash google-conta.sh
   ```
   Ele mostra o **URI de redirecionamento**, algo como `https://byte.<...>.azurecontainerapps.io/oauth/google`. Deixe o
   script esperando e vá para o próximo passo.
5. No Google Cloud: **Google Auth Platform → Clientes → Criar cliente**:
   - Tipo: **Aplicativo da Web**.
   - **URIs de redirecionamento autorizados:** cole o endereço do passo 4.
   - Crie e copie o **ID do cliente** e a **Chave secreta do cliente** (começa com `GOCSPX-`).
6. Volte ao script e cole os dois. A chave secreta não aparece na tela e vai para os Segredos do contêiner.

## 2. Conectar a sua conta
No Telegram do agente (ou no app, em Comandos → 🔑 Conectar o Google):
```
/google entrar
```
Abra o link, entre com a sua conta e aceite. Se aparecer **"O Google não verificou este app"**, clique em
**Avançado → Acessar Byte (não seguro)**: o app é o seu próprio projeto. O agente avisa quando terminar. Repita para
outra conta Google, se quiser.

## Uso
- `/compromissos` mostra Google, Outlook e iCal juntos. O resumo da manhã, o aviso de compromisso, o "hora de sair" e
  os horários livres também usam o Google.
- "Marca uma call com a@b.com sexta às 10h no Google, com Meet". **Sempre pede aprovação**, porque o convite sai para
  outras pessoas.
- `/google padrao google` (ou `outlook`) escolhe onde os compromissos novos são criados.
- `/google drive off` desliga o Drive automático. `/google sair` desconecta.

## O que o agente pode acessar
- **Agenda:** só os compromissos (`calendar.events`).
- **Drive:** só os arquivos que **ele mesmo criou** (`drive.file`). Ele não vê o resto do seu Drive.
- Para revogar o acesso: myaccount.google.com → Segurança → Apps de terceiros → Byte.
