# App Kronus para Android (nativo)

Diferente do app da página `/voz`, o app nativo consegue usar o **microfone e o alto-falante do relógio Bluetooth**
(como numa ligação) e pode ser o **assistente padrão** do Android, aberto pelo botão de assistente do relógio.

## Instalar
1. No celular, baixe: https://github.com/DragonClaw121/byte-agent/releases/download/app-kronus/kronus.apk
2. Abra o arquivo baixado. O Android pede para **permitir instalar apps desta fonte** (Chrome ou Arquivos): permita
   e toque em **Instalar**. Se aparecer o aviso do Play Protect, toque em **Instalar mesmo assim** (o app não vem da
   Play Store, por isso o aviso).
3. Abra o **Kronus** e permita **microfone** e **dispositivos por perto** (Bluetooth).

## Conectar (uma vez)
1. No Telegram do Kronus, mande `/parear`.
2. **Segure** o link que ele enviar → **Copiar**.
3. No app, toque em **Colar do Telegram**. Pronto.

## Usar com o relógio
- Com o relógio conectado ao celular, deixe marcado **"Usar microfone e alto-falante do relógio"**: você fala no
  relógio e a resposta sai nele.
- Para o **botão de assistente** do relógio (ou segurar o botão do celular) abrir o Kronus já ouvindo:
  Configurações do Android → **Apps** → **Apps padrão** → **App assistente digital** (ou "Assistência e entrada de
  voz") → escolha **Kronus**. Em alguns celulares a opção fica em "Assistente de voz".
  Se o Kronus não aparecer nessa lista, o atalho ainda funciona pelo ícone do app.

## Som no relógio (canal de ligação)
Relógios como o Jiditech só aceitam som no "canal de ligação", e o celular só os reconhece como fone por instantes:
o canal abre e fecha sozinho. Nos testes, só a voz do Android falando direto chegou ao relógio. Por isso o app fala a
resposta **frase por frase** (método **D**), conferindo o canal antes de cada trecho e reabrindo quando ele fechou.
O método **D0** fala o texto inteiro de uma vez. Toque em **Testar som do relógio** (1 bipe = D, 2 = D0) e escolha o
que saiu mais inteiro. Nesses relógios a voz no pulso é a do Android, não a do Azure (que segue no Telegram e no
painel).

## Atualizar
Cada versão nova do app é publicada no mesmo link. Baixe e instale por cima: a conexão é mantida.

## Segurança
- Cada celular tem acesso próprio: `/dispositivos` lista, `/desparear <id>` corta na hora.
- O app só fala com o endereço HTTPS do seu Kronus.
- A assinatura do APK usa uma chave "debug" guardada no repositório (permite atualizar por cima). Ela é pública:
  instale o APK **somente** do link acima. Para assinatura privada, crie no GitHub os segredos do repositório e troque
  o `signingConfig` (ver comentário em `android/app/build.gradle`).
