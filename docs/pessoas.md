# Pessoas: cada uma com o próprio espaço (v13.42)

Você pode deixar outras pessoas usarem os agentes (família, amigos, a equipe de uma empresa). Cada pessoa tem um
**espaço próprio**:

| Fica separado por pessoa | Continua só seu (do dono) |
|---|---|
| conversa e histórico | suas caixas de e-mail |
| memória ("lembra que eu...") | suas contas Microsoft e Google |
| tarefas, lembretes e rotinas diárias | suas aprovações |
| objetivos (tarefas longas) | executar código, trocar modelo, atualizar |
| reuniões e atas, arquivos gerados | `/saude`, `/permissoes` e o painel completo |
| resumo da manhã e da semana | os dados de qualquer outra pessoa |

O agente chama a pessoa pelo nome e não conhece os seus dados. Tecnicamente, cada espaço é um banco separado em
`<dados do agente>/pessoas/<n>/`. Ele entra na cópia de segurança da nuvem a cada 5 minutos, como o resto.

Cada agente tem a própria lista. Quem você convidar pelo Kronus usa o Kronus, e quem você convidar pelo Bit usa o Bit.

## Convidar alguém

**Pelo Telegram (ou pela aba Comandos do app):**

```
/convidar Ana
```

O agente responde com um link assim: `https://t.me/<seu_bot>?start=...`. Mande esse link para a pessoa. Ela toca nele,
aperta **Iniciar** e já entra no espaço dela. O convite vale **7 dias** e só **uma vez**.

**Pelo painel:** aba **Agenda** → **👥 Pessoas**. Digite o nome e clique em **✉️ Convidar**. Em cada pessoa, o painel
também tem os botões **🖥️ Painel** (link do painel pessoal), **✉️ Convite** (convite novo), **📊 Limite** e
**🚫 Remover**.

## Painel pessoal (sem Telegram)

Cada pessoa pode usar um painel no navegador (celular ou computador), com conversa e todos os comandos dela em botões:

```
https://<endereço>/u/kronus#<código pessoal>
```

O link é curto e o endereço é sempre o mesmo. Só o código depois do `#` muda de pessoa para pessoa. Esse código não é
enviado em nenhum pedido ao servidor: a página o guarda no aparelho e tira da barra de endereço.

- **A pessoa pede o próprio link:** manda `/painel` no Telegram.
- **Você gera o link:** use `/pessoas painel <n>` ou o botão **🖥️ Painel**. Serve para quem não usa Telegram.
- **Link novo:** pedir de novo desliga o link anterior. Se um link vazar, gere outro.

Para um endereço ainda mais simples (`https://assistente.suaempresa.com.br/u/bit`), veja
"Domínio próprio" em [azure-conteiner.md](azure-conteiner.md).

## App "Meu Assistente" para Android (v13.46)

É um app sem a marca do Kronus, para quem foi convidado: https://github.com/DragonClaw121/byte-agent/releases/download/app-assistente/assistente.apk

1. A pessoa instala o APK. O Android pede para permitir "instalar de fontes desconhecidas".
2. Abre o app e cola o link do painel pessoal. Ela recebe o link com `/painel`, que já vem com o link do app.
   Também dá para usar **Compartilhar → Meu Assistente** no link.
3. Pronto. O app mostra o painel dela (conversa, comandos e e-mail) e entrega os avisos como notificação:
   lembretes, resumos, atas e e-mails. Ele consulta o servidor a cada minuto.

No menu **⋯** dá para desligar as notificações ou trocar o link. Se o link for trocado ou o acesso removido, o app
avisa e pede o link novo.

## Papéis

- **membro** (padrão): usa o próprio espaço.
- **admin**: além disso, convida e remove **membros**, com `/convidar`, `/remover`, `/limite` e `/pessoas`. Não cria
  outros admins e não vê os dados de ninguém. Serve para delegar a gestão da equipe numa empresa.

```
/convidar Carla admin
```

## Limite diário (para proteger a sua cota)

As mensagens dos convidados usam a **sua** Azure (modelo) e o **seu** Groq (transcrição). Por isso, cada pessoa tem
um limite de mensagens por dia:

- **Padrão:** 150 mensagens por dia. Para mudar o padrão, defina a variável `BYTE_LIMITE_PESSOA_DIA` no contêiner.
- **Por pessoa:** `/limite <n> <mensagens>`, por exemplo `/limite 2 300`.
- **Renovação:** o limite renova à meia-noite. `/ajuda`, `/eu`, `/painel` e `/limpar` não contam.
- **Quem chega ao limite:** a pessoa recebe "⏳ Você usou as N mensagens de hoje".
- **Como acompanhar:** a pessoa vê o próprio uso com `/eu`; você vê o de todos com `/pessoas`.

## Remover

- **`/remover <n>`** tira o acesso (Telegram e painel) na hora. Os dados ficam guardados.
- **`/remover <n> apagar`** tira o acesso e apaga tudo da pessoa.

## Comandos

| Quem | Comandos |
|---|---|
| Dono | `/pessoas` · `/convidar <nome> [admin]` · `/pessoas painel <n>` · `/pessoas convite <n>` · `/limite <n> <msgs>` · `/remover <n> [apagar]` |
| Convidado | `/ajuda` · `/agenda` · `/tarefa` · `/feito` · `/lembrete` · `/lembretes` · `/cancelar` · `/rotina` · `/memorias` · `/lembrar` · `/objetivo` · `/objetivos` · `/resumo` · `/semana` · `/clima` · `/onde` · `/buscar` · `/pesquisar` · `/reunioes` · `/ata` · `/painel` · `/eu` · `/limpar`, além de notas de voz e áudios de reunião com a legenda "ata". No Bit, também `/arquivos`, `/analise`, `/apresentacao` e `/modelos` |

## E-mail de cada pessoa (v13.45)

No Bit e na Matrix, cada convidado pode ligar as **próprias** caixas de e-mail. Elas ficam no espaço dele, separadas
das suas:

- **Onde:** painel pessoal → aba **✉️ E-mail** → endereço + senha de app → **Adicionar e testar**.
- **Gmail, Yahoo e iCloud:** precisam de **senha de app** (no Gmail: myaccount.google.com/apppasswords, com a
  verificação em 2 etapas ligada).
- **E-mail da empresa (qualquer domínio):** a senha da caixa ou a senha de app, conforme o provedor.
- **Outlook, Hotmail e Microsoft 365:** sem senha; botão **Entrar pela Microsoft** (usa o mesmo aplicativo da
  Microsoft que você já configurou, `BYTE_MS_CLIENT_ID`). Contas de empresa podem precisar da aprovação da TI.
- **Segurança:** a senha nunca passa pelo chat (`/email senha ...` é recusado) e fica **criptografada** no espaço da
  pessoa com a chave `BYTE_CHAVE_TOKENS`. Sem essa chave, a senha vale só até a nuvem reiniciar.
- **O que o convidado não vê:** as suas contas (`BYTE_EMAIL_CONTAS`) e as suas senhas. O servidor de e-mail também não
  pode apontar para endereços internos da nuvem.
- **Comandos do convidado:** `/email`, `/caixa`, `/abrir`, `/triagem`. As respostas continuam com o cartão de
  aprovação, que a própria pessoa aprova.

## O que ainda não existe (próximos passos)

- O convidado ainda não conecta o próprio **Google Agenda/Drive** nem a **agenda do Outlook** (só o e-mail).
- Cada espaço é um agente completo rodando junto do seu. Isso funciona bem para dezenas de pessoas. Para centenas de
  usuários (uma empresa grande), o próximo passo é carregar o espaço só quando a pessoa usa e separar a cobrança por
  pessoa.
