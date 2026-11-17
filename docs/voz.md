# Comando de voz

Todos os agentes atendem por voz. A diferença está em quem pode falar e em como cada um responde:

| Agente | Quem pode comandar por voz | Como responde |
|---|---|---|
| **Matrix** | o dono (painel, Telegram, `/voz`) | até 4 frases claras; áudio quando a pergunta veio em áudio |
| **Bit** | o dono | começa pela conclusão e resume em voz; números e tabelas ficam no texto |
| **Kronus** | o dono | até 2 frases, sempre com áudio; a voz é o canal principal (relógio e celular) |
| **Byte** | **só o dono**: chat privado do Telegram, painel ou `/voz` | 1 a 3 frases faladas; com a live ligada, responde **na live**, falando com você |

## Onde falar

### Painel (todos)
Botão **🎙️ Falar** ao lado de "Enviar". O navegador reconhece a fala (Chrome e Edge fazem isso sem configurar nada)
e a resposta é lida em voz alta pela voz do próprio navegador. Opções embaixo do campo:

- **Mãos livres:** o microfone fica aberto e o agente só reage quando você o chama pelo nome no começo da frase
  ("Kronus, que horas são?"). Dizer só o nome abre a escuta para o comando seguinte.
- **Ler as respostas de voz em voz alta:** desligue para receber só o texto.

Em navegadores sem reconhecimento de fala (Firefox), o painel grava o áudio e o servidor transcreve, se a transcrição
estiver configurada (abaixo).

### Telegram (todos)
Mande uma **nota de voz**. O agente responde com "🎙️ Ouvi: ...", a resposta em texto (com os botões de aprovação,
quando houver) e, se a fala das respostas estiver configurada, uma nota de voz. Precisa de transcrição configurada.

### Página `/voz` (celular e relógio; principal canal do Kronus)
Abra `http://SEU_SERVIDOR:PORTA/voz` no celular (pela rede de casa ou pelo Tailscale) e cole o token da API do
agente (`KRONUS_API_TOKEN`). O token fica só naquele aparelho. Dá para abrir já conectado com
`/voz#token=SEU_TOKEN` (o `#` não vai para o servidor e a página apaga o token da barra de endereço).

- Toque no botão grande e fale; ou ligue **Mãos livres** e chame "Kronus, ...".
- **Ler avisos:** lê em voz alta lembretes e iniciativas a cada 30 s enquanto a página estiver aberta.
- No Android e no iPhone, "Adicionar à tela inicial" deixa a página com cara de aplicativo.
- A página funciona em telas de relógio (testada a 240×280), mas navegadores de relógio raramente têm microfone e
  reconhecimento de fala. No relógio, o caminho mais confiável é um atalho que dita o texto e chama a API:
  `POST /api/v1/voice` com `{"text": "..."}` (Atalhos do iPhone/Apple Watch, Tasker no Android/Wear OS).

A página precisa da API do agente ligada (`canais.api` no manifesto; o Kronus já vem com ela). No Colab o servidor
fica dentro da máquina do Google: use a página com o agente rodando no seu PC ou servidor (`python -m byte_agent
--agent kronus`).

## A Byte só obedece a você

- No Telegram, ela só aceita voz do **chat privado** com um id autorizado; áudio vindo de grupo é recusado.
- No painel e na página `/voz`, quem fala é quem tem o token, ou seja, você.
- **Durante a live**, a sua voz vira uma fala dela na live: ela fura a fila do chat, responde falando com você (o chat
  ouve) e atende mesmo com a live pausada para o chat (`/live off`). Mensagens do chat da Twitch continuam sendo só
  texto, com os filtros de sempre: ninguém do chat consegue mandar voz para ela.
- Use **Mãos livres** no painel durante a live: ela só reage quando você diz "Byte, ..." e ignora o resto do que
  você fala com o chat.
- Ela **não** escuta o áudio da live, do jogo ou de convidados. Não há reconhecimento de quem está falando pelo timbre:
  a garantia vem do canal (só você tem o token e o chat privado). Num microfone compartilhado, prefira o botão
  (aperte para falar) em vez do modo mãos livres.

## Transcrição (fala para texto)

Necessária para o Telegram, para áudio enviado à página `/voz` e para o painel em navegadores sem reconhecimento.
Escolha uma opção e coloque os valores como Segredos do Colab ou variáveis de ambiente:

| Opção | Configuração | Custo |
|---|---|---|
| **Groq** (recomendado para começar) | `BYTE_STT_URL=https://api.groq.com/openai/v1`, `BYTE_STT_MODEL=whisper-large-v3-turbo`, segredo `BYTE_STT_API_KEY` | camada gratuita |
| OpenAI | `BYTE_STT_URL=https://api.openai.com/v1`, `BYTE_STT_MODEL=whisper-1`, segredo `BYTE_STT_API_KEY` | pago por minuto |
| Local, sem internet | `pip install faster-whisper` (`BYTE_STT_MODEL=small`) | grátis; usa GPU/CPU junto com o Ollama |
| Servidor próprio | qualquer servidor compatível com `/audio/transcriptions` (faster-whisper-server, LocalAI) | grátis |

No Colab com GPU pequena, prefira o Groq: o Whisper local disputa a VRAM com o modelo do Ollama.

## Fala das respostas (texto para voz)

O painel e a página `/voz` já falam com a voz do navegador/celular. Configurar uma voz no servidor serve para mandar
**nota de voz no Telegram** e para ter a mesma voz em todo lugar:

| Opção | Configuração |
|---|---|
| Kokoro-FastAPI (local, tem voz pt-BR) | `BYTE_VOICE_TTS_URL=http://127.0.0.1:8880/v1`, `BYTE_VOICE_TTS_MODEL=kokoro`, `BYTE_VOICE_TTS_VOICE=pf_dora` |
| OpenAI | `BYTE_VOICE_TTS_URL=https://api.openai.com/v1`, `BYTE_VOICE_TTS_MODEL=tts-1`, segredo `BYTE_VOICE_TTS_API_KEY` |
| Piper (o mesmo da live) | `BYTE_TTS_COMMAND=...` (com `ffmpeg` instalado vira nota de voz; sem ele, vai como arquivo `.wav`) |

## Regras no manifesto

```json
"voz": {
  "ativo": true,
  "responder_com_voz": "auto",
  "palavras_chave": ["byte", "bait"],
  "somente_dono": true,
  "na_live": true
}
```

- `responder_com_voz`: `auto` (áudio quando a pergunta veio em áudio), `sempre` ou `nunca`.
- `palavras_chave`: nomes que ativam o modo mãos livres. Inclua as grafias que a transcrição costuma errar
  ("cronos" para Kronus, "bait" para Byte). Sem a lista, vale o nome do agente.
- `somente_dono`: recusa voz de grupos do Telegram.
- `na_live`: só para `vtuber`; com a live ligada, a voz do dono é respondida na live.

## Segurança

- Voz nunca aprova nada. E-mails, Asana, Notion e Teams continuam esperando o clique em Aprovar.
- O que é dito por voz passa pelas mesmas regras do chat, inclusive o bloqueio de gravação depois de ler a internet.
- Os áudios não ficam salvos: são transcritos e descartados. Com Groq ou OpenAI, o áudio sai da sua máquina para o
  serviço escolhido; use a opção local se isso importar.

## API

```bash
curl -X POST http://SEU_SERVIDOR:8772/api/v1/voice \
  -H "Authorization: Bearer $KRONUS_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "Kronus, o que tenho amanhã?"}'
```

Também aceita o áudio direto no corpo (`Content-Type: audio/webm`, `audio/ogg`, `audio/mpeg`...) ou
`{"audio_b64": "...", "mime": "audio/webm"}`. Resposta: `heard`, `reply`, `speech` (texto limpo para falar),
`audio_b64`/`audio_mime` (quando há voz configurada), `ignored`/`wake_only` (modo mãos livres, com
`"hands_free": true`) e `pending_approvals`.
