# Byte ao vivo: Twitch + VTube Studio + voz

Fluxo: espectador escreve no chat → a Byte decide se a mensagem é para ela → a LLM local gera uma fala curta
com uma etiqueta de emoção → a fala vai para o chat, o VTube Studio troca a expressão, o TTS fala e a boca do
modelo acompanha o áudio.

Tudo aqui foi testado com servidores simulados. **Twitch, VTube Studio e Piper reais ainda não foram testados**:
siga os passos na ordem e valide cada um antes do próximo.

## O que a live NÃO pode fazer (de propósito)

O chat é público. Por isso as respostas da live usam um caminho isolado da LLM:

- sem ferramentas (nada de código, shell, e-mail, agenda ou lembretes);
- sem suas memórias, sua agenda e seu perfil pessoal no prompt;
- o texto dos espectadores entra marcado como conteúdo de terceiros;
- a saída passa por filtro: termos bloqueados, e-mails/telefones/CPF, links, tamanho;
- o que vai para o chat nunca começa com `/`, `.` ou `!` (não vira comando da Twitch nem de outros bots).

Um espectador pode, no máximo, fazer a Byte falar bobagem. Para isso existem a lista de bloqueio e o botão de
emergência `/live off` no Telegram.

## Passo 1 — Conta e token da Twitch

1. Decida a conta que vai falar no chat: a sua ou uma conta separada para o bot.
2. Em <https://dev.twitch.tv/console> registre um aplicativo (redirect `http://localhost:3000`) e anote o
   **Client ID** e o **Client Secret**.
3. Gere o token de usuário, logado na conta do bot, com os escopos `chat:read chat:edit`. Com a CLI oficial da Twitch:

   ```bash
   twitch configure
   ```

   ```bash
   twitch token -u -s "chat:read chat:edit"
   ```

   Ela mostra o **User Access Token** e o **Refresh Token**.
4. Preencha no `.env`: `BYTE_TWITCH_CHANNEL`, `BYTE_TWITCH_NICK`, `BYTE_TWITCH_TOKEN`, `BYTE_TWITCH_CLIENT_ID`,
   `BYTE_TWITCH_CLIENT_SECRET`, `BYTE_TWITCH_REFRESH_TOKEN`.

O token de acesso dura poucas horas. Com os três últimos campos a Byte renova sozinha e guarda os novos tokens
em `<BYTE_DATA_DIR>/twitch_tokens.json` (o refresh token da Twitch é de uso único). Sem eles, a ponte para
quando o token expirar e avisa no log.

## Passo 2 — VTube Studio

1. Abra o VTube Studio com o seu modelo. No Linux ele roda pela Steam com Proton; a API é local (porta 8001).
2. Configurações → ative **Start API (allow plugins)**.
3. Crie um hotkey para cada expressão que quiser usar (por exemplo `Happy`, `Sad`, `Angry`, `Surprised`) e um
   que volte ao normal (`Neutral`, do tipo "remover todas as expressões").
4. No `.env`: `BYTE_VTS_URL=ws://127.0.0.1:8001` e o mapa em `BYTE_STREAM_EMOTIONS`.
5. Liste os hotkeys que a Byte enxerga (na primeira vez o VTube Studio pede para autorizar o plugin "Byte Agent"):

   ```bash
   python -m byte_agent --vts-hotkeys
   ```

Emoções padrão: `neutra, feliz, triste, surpresa, brava, pensativa, envergonhada`. Só as que estiverem no mapa
disparam hotkey; as outras apenas não mexem no avatar.

## Passo 3 — Voz (TTS)

Qualquer programa que receba texto e grave um WAV serve. Exemplo com o Piper e uma voz pt_BR
(arquivos `.onnx` e `.onnx.json` da voz na mesma pasta):

```bash
echo "Olá, chat!" | piper --model /caminho/pt_BR-faber-medium.onnx --output_file teste.wav
```

No `.env`:

```
BYTE_TTS_COMMAND=piper --model /caminho/pt_BR-faber-medium.onnx --output_file {out}
```

O texto nunca passa por um shell: vai pela entrada padrão do comando.

## Passo 4 — Testar avatar e voz sem a Twitch

```bash
python -m byte_agent --say "[feliz] Oi, chat! Este é um teste."
```

Deve tocar a voz, abrir e fechar a boca do modelo e disparar o hotkey de `feliz`. Se a boca não mexer, confira
se o parâmetro `MouthOpen` está ligado à boca no seu modelo (ou ajuste `BYTE_VTS_MOUTH_PARAM`).
Alternativa sem injeção: deixar o próprio VTube Studio fazer a sincronia labial pelo "microfone", apontando-o
para um dispositivo virtual que recebe o áudio do TTS, e usar `BYTE_AUDIO_PLAYER` para tocar nesse dispositivo.

## Passo 5 — OBS

1. Fonte de captura da janela do VTube Studio (ative o fundo transparente nele, ou use chroma key).
2. Captura do áudio de saída do sistema, para a voz da Byte entrar na transmissão.

## Passo 6 — Ligar

```bash
python -m byte_agent
```

O log mostra `Live ativa: Twitch #canal (modo mention) | VTube Studio | voz`. No chat, escreva `Byte, tudo bem?`.

Pelo Telegram: `/live` mostra o estado, `/live off` pausa na hora e esvazia a fila, `/live on` retoma.

## Ajustes

| Variável | Efeito |
|---|---|
| `BYTE_STREAM_MODE` | `mention` (padrão), `command` (só `!byte ...`) ou `all` (toda mensagem) |
| `BYTE_STREAM_IDLE_S` | segundos de silêncio até ela puxar assunto sozinha; `0` desliga. Para depois de 4 falas seguidas sem ninguém no chat |
| `BYTE_STREAM_PERSONA_FILE` | arquivo de texto com a personalidade. Não coloque dados pessoais: esse texto vai para o prompt da live |
| `BYTE_STREAM_BLOCKLIST_FILE` | um termo por linha; mensagens e respostas que contenham algum são descartadas |
| `BYTE_STREAM_MODEL` | modelo usado na live (por padrão, o modelo ativo) |
| `BYTE_STREAM_REPLY_IN_CHAT=0` | responde só pela voz |

## Antes de ir ao ar

- Você responde pelo que a Byte disser no seu canal: leia as regras da Twitch sobre bots e conteúdo.
- Não dê cargo de moderador à conta do bot.
- Preencha a lista de bloqueio e mantenha o AutoMod do canal ligado.
- Comece em `mention`, com `BYTE_STREAM_IDLE_S=0`, e teste em uma transmissão fechada.
- Chat pessoal (Telegram/painel) e live dividem a mesma GPU: uma resposta espera a outra.
