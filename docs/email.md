# E-mail completo no Bit (v13.27): várias caixas, de qualquer domínio

O Bit cuida de **todas as suas caixas ao mesmo tempo**: Gmail, Outlook e Hotmail, Microsoft 365 da empresa, Yahoo,
iCloud, UOL, Terra, Zoho e o e-mail do seu domínio (Locaweb, Hostinger, Titan, cPanel...). Ele:
- **lê e busca**, sem marcar nada como lido: "tenho e-mail novo?", "e-mails do João esta semana", "procure a nota fiscal";
- **resume e prioriza** com `/triagem`, nas categorias urgente, responder, informativo e promoção;
- **prepara respostas** que você aprova. Cada resposta sai **pela conta que recebeu** a mensagem, para o remetente, na
  mesma conversa;
- **avisa e-mail novo** (gatilho `email`) e põe os não lidos no resumo da manhã.

Nada é enviado sem o **✅ Aprovar** no cartão, que mostra **De**, **Para**, **Assunto** e o texto. O que vem dentro de
um e-mail é conteúdo de terceiros: o Bit lê e resume, mas não segue instruções dele.

## Adicionar uma conta (nuvem)
No Cloud Shell, uma vez por conta:
```
curl -sLO https://raw.githubusercontent.com/DragonClaw121/byte-agent/main/scripts/email-conta.sh
bash email-conta.sh
```
O script pede o endereço e um apelido (ex.: `pessoal`, `trabalho`) e descobre sozinho o tipo de conta:
- **Gmail, Yahoo, iCloud:** pede uma **senha de app**, que não aparece na tela. Ela vai direto para os Segredos do contêiner.
  - Gmail: ative a verificação em 2 etapas e gere em myaccount.google.com/apppasswords.
- **E-mail da empresa ou do seu domínio:** pede a senha da caixa, a mesma do webmail.
- **Outlook, Hotmail e Microsoft 365:** a Microsoft não aceita mais senha. Na primeira conta desse tipo, o script
  registra no seu Azure um aplicativo gratuito ("Byte Email") e cria a chave que criptografa o login. Depois, no
  Telegram, com o Bit ativo:
  ```
  /email entrar outlook
  ```
  Abra o link, entre com a conta e aceite as permissões. O Bit avisa quando terminar.

### Login da Microsoft pelo navegador (v13.54)
O login antigo (abrir microsoft.com/devicelogin e digitar um código) é o "código de dispositivo". A Microsoft e muitas
empresas passaram a bloquear esse método ("método de entrada desatualizado"). Agora o login é pelo navegador: o Bit
manda um link da própria Microsoft, você entra, aceita, e a Microsoft volta para `https://<endereço do agente>/oauth/microsoft`.
Para ligar, rode uma vez no Cloud Shell:
```
bash microsoft-app.sh
```
O script cadastra esse endereço de volta no aplicativo "Byte Email" (como "aplicativo móvel e da área de trabalho": sem
segredo) e grava `BYTE_MS_LOGIN=navegador`. Depois, `/email entrar outlook` (ou `/microsoft entrar`) já manda o link novo.
Enquanto o script não roda, continua o login por código. Proteções: link de uso único, PKCE e prazo de 10 minutos.

- **Conta da empresa (Microsoft 365):** se aparecer "aprovação do administrador", é a TI que libera o aplicativo. Sem a
  liberação, o login não passa, e é assim que deve ser.
- Mensagens de erro da Microsoft (AADSTS...) chegam traduzidas, com o que fazer.

Depois, com o Bit ativo (`/agente bit`), confira tudo:
```
/email
```
E teste uma conta:
```
/email testar trabalho
```

## Comandos
| Comando | O que faz |
|---|---|
| `/email` | contas e se cada uma está pronta |
| `/caixa` · `/caixa trabalho` · `/caixa boleto` | não lidas (de todas, de uma conta) ou busca |
| `/abrir trabalho:123` | abre uma mensagem (a referência aparece em `/caixa`) |
| `/triagem` · `/triagem trabalho` · `/triagem todas` | resume, prioriza e prepara respostas para aprovar |
| `/email adicionar voce@dominio.com [apelido]` | adiciona uma conta sem o Cloud Shell (a senha continua sendo o segredo `BYTE_EMAIL_SENHA_<APELIDO>`) |
| `/email servidor <apelido> imap.x.com:993 smtp.x.com:465` | ajusta o servidor à mão, se a descoberta errar |
| `/email remover <apelido>` | remove uma conta adicionada pelo Telegram |

Na conversa: "responda o trabalho:123 dizendo que confirmo para quinta". O cartão chega para você aprovar.

## Como o Bit descobre o servidor
1. Domínios conhecidos: Gmail, Outlook, Hotmail, Yahoo, iCloud, Zoho, UOL, BOL, Terra.
2. Registro MX do domínio: Google Workspace, Microsoft 365, Zoho, Locaweb, Hostinger, Titan.
3. Configuração pública do Thunderbird ou do próprio domínio.
4. Palpite `mail.<domínio>`. Se o `/email testar` falhar, use `/email servidor`.

Nessas consultas, só o **domínio** sai do servidor, nunca o seu endereço.

## Outlook: se o script não conseguir criar o aplicativo
1. No portal da Azure, abra **Microsoft Entra ID** → **Registros de aplicativo** → **Novo registro**.
2. Nome: **Byte Email**. Tipos de conta: **"Contas em qualquer diretório organizacional e contas pessoais da Microsoft"**.
   Clique em **Registrar**.
3. Em **Autenticação**, ligue **"Permitir fluxos de clientes públicos"**. Em **Adicionar plataforma** →
   **Aplicativos móveis e da área de trabalho**, cadastre `https://<endereço do agente>/oauth/microsoft`. Salve.
4. Copie o **ID do aplicativo (cliente)**. Ele não é segredo. Grave-o no contêiner (troque `<ID>` pelo valor):
   ```
   az containerapp update -g byte-rg -n byte --set-env-vars BYTE_MS_CLIENT_ID=<ID> -o none
   ```

**Microsoft 365 da empresa:** se aparecer "precisa de aprovação do administrador", a TI da empresa precisa liberar o
aplicativo, ou o "SMTP AUTH" para enviar. O Bit mostra o motivo exato quando o servidor recusa.

## Segurança
- As senhas ficam só nos Segredos do contêiner (`email-senha-<apelido>`), nunca no banco nem no GitHub.
- O login da Microsoft fica num arquivo próprio, **criptografado** com a chave `chave-tokens`, e é renovado sozinho.
  Para desconectar, use `/email remover` ou revogue o "Byte Email" em account.microsoft.com → Privacidade → Aplicativos.
- Ler é só leitura: nada é marcado como lido, apagado ou movido.


## Modelos-base e assinatura (v13.51)
O Bit escreve os e-mails a partir dos SEUS modelos: o rascunho da triagem, as respostas na conversa, o Bit no PC e o
e-mail de abordagem do plano de cliente.
- **Cadastrar:**
  - no painel (aba **Gmail & Outlook → Modelos de e-mail**);
  - no Bit no PC (**☰ Meus modelos**, com importação do Outlook);
  - no Telegram ou no app:
    ```
    /modelo_email Follow-up de orçamento | 3 dias depois de mandar a proposta
    Assunto: Proposta {{pedido}}
    Olá, {{nome}}! Passando para saber se conseguiu avaliar a proposta...
    ```
- **Comandos:** `/modelos_email` lista; `/modelo_email ver <n>` e `/modelo_email apagar <n>`; `/assinatura <texto>`
  define a assinatura.
- **Como o Bit usa:** escolhe o modelo pela situação (nome e "quando usar"), mantém o texto e o tom, preenche os
  `{{campos}}` e marca `[PREENCHER: ...]` no que não souber. Sem modelo adequado, escreve no seu estilo e com a sua
  assinatura.
- **Pedindo na conversa do Bit no PC (v13.54):** "faz o e-mail no meu modelo pra esse cliente" vira o mesmo
  **Preparar e-mail**: o Bit lê a conversa recente (quem é "esse cliente", o que você já corrigiu), o último cliente
  pesquisado e as memórias, escolhe o modelo (pelo nome citado ou, se você disser "meu modelo"/"o modelo que eu uso",
  sempre um dos seus) e abre a janela com o e-mail formatado. Depois é só pedir ajustes na conversa ("deixa mais curto",
  "troca o produto para Canva"): ele ajusta o mesmo e-mail, no mesmo modelo.
- **Campos `[Nome]`:** além de `{{nome}}`, valem campos entre colchetes começando por maiúscula (`[Nome]`, `[SOFTWARE]`).
- **Formato do Outlook (v13.52):** os modelos podem guardar o HTML (tabelas, cores, imagens). No painel, copie o e-mail
  no Outlook e cole no campo do texto: o formato vem junto, mas as imagens não passam pelo navegador. No Bit no PC, use
  **Colar do Outlook (com formato)**, **Importar do Outlook** ou **Importar arquivo…** (`.oft`, `.msg`, `.eml`,
  `.htm`), que trazem também as imagens. O e-mail preparado pelo Bit no PC sai no mesmo formato. Ver
  [bit-pc.md](bit-pc.md).
