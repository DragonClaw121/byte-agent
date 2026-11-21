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

## Kronus te liga (v1.11)
Ligue **"📞 Kronus pode me ligar para avisar"**. O app fica atento aos avisos do servidor (uma notificação discreta
"Kronus atento" fica fixa; é exigência do Android) e:
- **lembretes** e **compromissos chegando** tocam como **chamada recebida**; atenda no relógio (se ele aceitar
  atender chamadas de apps) ou na notificação do celular. O Kronus lê o aviso com a voz dele e, depois do bipe, ouve a
  sua resposta (ex.: "me lembra de novo em 10 minutos");
- se ninguém atender em 30 s, vira **chamada perdida** com o texto na notificação; os demais avisos chegam como
  notificação comum (dá para incluí-los nas chamadas). O Telegram continua recebendo tudo.
- **Teste:** "Testar: Kronus me liga (em 5 s)".
- **Resumo como ligação (v1.12):** ligue "Resumo da manhã e fechamento da semana (lidos em voz)" para o resumo
  das 7h tocar como chamada e o Kronus ler uma versão falada, curta e sem listas. Vem desligado. Veja [resumos.md](resumos.md).
- **Xiaomi/Redmi/Poco:** em Configurações → Apps → Kronus, ligue **Início automático** e em **Economia de bateria**
  escolha **Sem restrições**; senão o sistema fecha o app e as chamadas não chegam. No app do relógio, permita as
  notificações do Kronus para ele vibrar no pulso.

## Localização (v1.13)
Ligue **"📍 Compartilhar minha localização aproximada"**. O Android pede a permissão de localização aproximada
(rede/Wi-Fi, cerca de 1 km). Com isso, o clima, o resumo da manhã e as sugestões passam a seguir **onde você está**.
- A posição é enviada ao abrir o app e, com "Kronus pode me ligar" ligado, a cada 15 minutos.
- O servidor guarda **só a última posição**, sem histórico. O nome da cidade e do bairro vem do OpenStreetMap, com as
  coordenadas arredondadas.
- No Telegram: `/onde` mostra onde o Kronus acha que você está, e `/clima` mostra o tempo de lá.
- Desligue a opção quando quiser: o Kronus volta a usar a cidade do resumo (`/resumo cidade`).

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
