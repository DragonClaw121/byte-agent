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

## Som no relógio: o Kronus como "app de chamadas" (v1.8)
Relógios como o Jiditech só aceitam som em ligações, e fora de uma ligação o celular abre e fecha o canal sozinho
(a fala cortava). Agora, ao tocar no 🎙️, o app faz uma **chamada do próprio app** para o Kronus (como apps de voz
fazem; não usa a operadora nem custa nada): o Android liga o relógio e mantém o canal aberto até o fim.
- Fale; ele responde e a chamada encerra. Com "Continuar a conversa depois da resposta" ligado, ele volta a ouvir
  (um bipe avisa) e só aceita fala clara; silêncio ou desligar no relógio encerra.
- "Ouvir minha última gravação" toca no celular o que o app enviou: se o áudio estiver ruim, é o microfone do
  relógio; se estiver bom e a transcrição errar, troque o modelo de transcrição (abaixo).
- Métodos: **C1** = chamada + voz do Kronus (a do Azure); **C2** = chamada + voz do Android (padrão);
  **D/D0** = modo antigo, sem chamada. **Testar som do relógio** faz uma chamada de teste: 1 bipe = C1, 2 = C2.
- Se o Android recusar a chamada, o app usa o modo antigo sozinho.

## Reconhecimento de voz melhor
O microfone do relógio numa chamada tem qualidade de telefone. O modelo `whisper-large-v3` (sem "turbo") do Groq é
mais preciso nesse tipo de áudio e continua gratuito:
```
az containerapp update -g byte-rg -n byte --set-env-vars BYTE_STT_MODEL=whisper-large-v3 -o none
```

## Atualizar
Cada versão nova do app é publicada no mesmo link. Baixe e instale por cima: a conexão é mantida.

## Segurança
- Cada celular tem acesso próprio: `/dispositivos` lista, `/desparear <id>` corta na hora.
- O app só fala com o endereço HTTPS do seu Kronus.
- A assinatura do APK usa uma chave "debug" guardada no repositório (permite atualizar por cima). Ela é pública:
  instale o APK **somente** do link acima. Para assinatura privada, crie no GitHub os segredos do repositório e troque
  o `signingConfig` (ver comentário em `android/app/build.gradle`).
