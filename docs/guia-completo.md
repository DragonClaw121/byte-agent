# Projeto BYTE: instalação, uso e atualização (Windows + WSL)

Guia completo para rodar o projeto num PC com Windows, do zero. Vale para notebooks sem placa de vídeo dedicada
(o modelo roda no processador). Tempo total da primeira instalação: 40 a 60 minutos, a maior parte em downloads.

## O que roda onde

| Peça | Onde | Para quê |
|---|---|---|
| Ubuntu (WSL) | no notebook | o "Linux dentro do Windows" onde tudo roda |
| Ollama | no Ubuntu | o modelo de linguagem (o "cérebro") |
| Byte (este projeto) | no Ubuntu | os agentes, o painel, o Telegram e a voz |
| Tailscale | no Ubuntu e no celular | ligação segura do celular com o notebook, de qualquer lugar |
| Telegram | no celular | conversar com os agentes por texto e por áudio |

## Os agentes do Projeto BYTE

| Agente | Quem é | Onde usar | Voz |
|---|---|---|---|
| **Matrix** (padrão) | o núcleo, "mãe" dos agentes: impessoal, impecável, pé no chão | painel, Telegram | serena |
| **Byte** | VTuber e idol: energética, amiga do chat, competitiva e empática | painel, Telegram, live na Twitch | só obedece à voz do dono |
| **Bit** | conselheiro de trabalho: e-mails, planilhas, relatórios, apresentações | painel, Telegram | grave e pausado |
| **Kronus** | assistente de bolso, "brother": rápido, sarcasmo leve | celular e relógio (página `/voz`), Telegram | a voz é o canal principal |

Um único processo atende todos: você troca de agente no painel (seletor ao lado do título) ou no Telegram
(`/agente kronus`). Cada agente guarda as próprias conversas, memórias e agenda.

---

## Parte 1. Instalação (uma vez só)

### 1. Ubuntu dentro do Windows (WSL)

No **PowerShell como administrador**:
```
wsl --install -d Ubuntu
```
Reinicie o Windows. Abra "Ubuntu" no menu Iniciar e crie usuário e senha (a senha não aparece enquanto você digita).

Ainda no Windows, crie o arquivo de configuração de memória do WSL (abra o PowerShell normal):
```
notepad $env:USERPROFILE\.wslconfig
```
Cole e salve:
```
[wsl2]
memory=12GB
swap=8GB
networkingMode=mirrored

[experimental]
autoMemoryReclaim=gradual
```
Aplique com `wsl --shutdown` e abra o Ubuntu de novo. (`networkingMode=mirrored` só existe no Windows 11; no
Windows 10, apague essa linha.)

> **Importante:** daqui em diante, todo comando é no **Ubuntu**, sempre a partir da sua pasta pessoal (`cd ~`).
> Nunca trabalhe em `/mnt/c/...`: é lento e, em `C:\Windows\System32`, dá "Permission denied".

### 2. Programas básicos
```
sudo apt update && sudo apt install -y python3-venv git curl unzip qrencode
```

### 3. Ollama e o modelo
```
curl -fsSL https://ollama.com/install.sh | sh
```
```
ollama pull qwen2.5:3b
```
Confira com `ollama list`. O qwen2.5:3b (uns 2 GB) é o melhor equilíbrio num notebook sem placa de vídeo.

### 4. O código do projeto
```
cd ~ && git clone https://github.com/DragonClaw121/byte-agent.git
```
```
echo "alias byte='bash ~/byte-agent/scripts/byte.sh'" >> ~/.bashrc && source ~/.bashrc
```
Agora o comando `byte` funciona de qualquer pasta. (Se o repositório estiver desatualizado, aplique o zip mais
recente antes de seguir: Parte 3, caminho B.)

### 5. Segredos (arquivo `~/.byte-env`)

Fica na sua pasta pessoal, fora do projeto: nunca vai para o GitHub. Gere três tokens (rode três vezes e anote):
```
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```
Crie o arquivo:
```
nano ~/.byte-env
```
Cole e preencha (salve com Ctrl+O, Enter, saia com Ctrl+X):
```
# Painel com link fixo (1º token gerado)
export BYTE_UI_TOKEN="..."

# Telegram (passo 7)
export BYTE_TELEGRAM_TOKEN="..."
export BYTE_TELEGRAM_CHAT_IDS="..."

# Transcrição de áudio pelo Groq (gratuito: console.groq.com > API Keys > Create API Key)
export BYTE_STT_URL="https://api.groq.com/openai/v1"
export BYTE_STT_MODEL="whisper-large-v3-turbo"
export BYTE_STT_API_KEY="..."

# Pesquisa na internet (Tavily: app.tavily.com > API Keys; plano gratuito com cota mensal).
# Sem ela, a busca usa só a Wikipédia, que não acha empresas, notícias nem preços.
export TAVILY_API_KEY="..."

# Kronus no celular (2º token gerado)
export KRONUS_API_TOKEN="..."

# Avisos no celular pelo app ntfy (3º token gerado; opcional)
export BYTE_NTFY_TOPIC="..."
```
Proteja o arquivo:
```
chmod 600 ~/.byte-env
```

### 6. Primeira vez ligando
```
byte iniciar
```
Na primeira vez ele cria o ambiente Python sozinho (alguns minutos). Abra no **Chrome do Windows**:
`http://127.0.0.1:8765/ui?token=SEU_BYTE_UI_TOKEN` e salve nos favoritos.

- A primeira resposta demora (30 s a 1 min): o modelo está sendo carregado. As seguintes são mais rápidas.
- **🎙️ Falar**: comando de voz em qualquer agente (permita o microfone).
- **Seletor de agentes** ao lado do título: Matrix, Bit, Byte, Kronus.

### 7. Telegram
1. No Telegram, fale com **@BotFather**, envie `/newbot`, escolha nome e usuário. Ele devolve o token: coloque em
   `BYTE_TELEGRAM_TOKEN`.
2. Deixe `BYTE_TELEGRAM_CHAT_IDS` vazio por enquanto, ligue a Byte (`byte iniciar`) e mande qualquer mensagem
   para o seu bot. Ele responde com o seu número ("Seu chat_id é ..."). Coloque esse número no arquivo.
3. `byte parar`, `byte iniciar`. Pronto: `/ajuda` lista os comandos.

| Comando | O que faz |
|---|---|
| `/agente` | mostra o agente ativo; `/agente matrix`, `trabalho`, `byte`, `bit`, `kronus` troca |
| nota de voz | o agente transcreve e responde |
| `/rotina 08:00 resuma minha agenda` | tarefa diária automática |
| `/lembrete 30m beber água` | lembrete |
| `/pesquisar <pergunta>` | pesquisa na web com fontes |
| `/status` | modelo, memória e estado |

> Não deixe a Byte ligada no Colab e no notebook ao mesmo tempo com o mesmo bot: as mensagens se perdem.

### 8. Tailscale (celular falando com o notebook)
No Ubuntu:
```
curl -fsSL https://tailscale.com/install.sh | sh
```
```
sudo tailscale up
```
Abra o link que aparecer e entre com a sua conta. Depois:
1. No site do Tailscale (login.tailscale.com), menu **DNS**: confirme **MagicDNS** ligado e clique em
   **Enable HTTPS** (uma vez só).
2. Publique a página de voz do Kronus (porta 8772, a do Kronus dedicado; passo 9):
   ```
   sudo tailscale serve --bg 8772
   ```
   Ele mostra o endereço (algo como `https://lspot-wks017.taile0ac68.ts.net/`). Isso fica ativo mesmo depois de
   reiniciar.
3. No **celular**: instale o app Tailscale, entre com a **mesma conta** e ligue.
   - Xiaomi/Redmi: **Configurações → Conexão e compartilhamento → DNS privado → Desativado**.
   - **Configurações → Apps → Tailscale → Economia de bateria → Sem restrições**.

Pelo Tailscale só a página de voz e a API do Kronus respondem; o painel completo abre apenas no notebook.

### 9. Kronus no celular (em paralelo com os outros agentes)
O Kronus roda num **processo próprio**, ao lado da instância principal (Matrix, Byte, Bit). Assim o celular fala com
ele o tempo todo, enquanto você usa os outros agentes no painel. Use **dois terminais** do Ubuntu (no Terminal do
Windows: `+` para abrir outra aba Ubuntu):

| Terminal | Comando | Painel no PC |
|---|---|---|
| 1 | `byte iniciar` (Matrix, Byte, Bit) | `http://127.0.0.1:8765/ui?token=SEU_BYTE_UI_TOKEN` |
| 2 | `byte kronus` | `http://127.0.0.1:8772/ui?token=SEU_BYTE_UI_TOKEN` |

- No painel principal, **não use** o Kronus enquanto o dedicado estiver ligado: a troca é recusada, para os dois
  processos não abrirem o mesmo banco de dados.
- O bot principal do Telegram continua com a instância principal. O Kronus dedicado só tem Telegram se você criar
  um bot só para ele (`KRONUS_TELEGRAM_TOKEN`); sem isso, ele atende pelo celular e pelo painel dele.
- Os dois usam o mesmo Ollama e o mesmo modelo (carregado uma vez só). Perguntas aos dois ao mesmo tempo dividem o
  processador e ficam mais lentas.

Com `byte kronus` ligado e o Tailscale apontando para a 8772 (passo 8):
1. No Ubuntu, gere um QR code com o endereço já conectado (troque pelo seu endereço do passo 8):
   ```
   source ~/.byte-env && qrencode -t ansiutf8 "https://SEU-ENDERECO.ts.net/voz#token=$KRONUS_API_TOKEN"
   ```
2. Leia com a câmera do celular. Depois rode `clear` (o QR contém o token).
3. No Chrome do celular: menu **⋮ → Adicionar à tela inicial**.
4. Toque no botão e fale, ou ligue **Mãos livres** e diga "Kronus, ...". Se o reconhecimento do celular falhar,
   a página passa sozinha a gravar e mandar o áudio para o Groq.

### 10. Opcional: vozes reais diferentes (Kokoro)
Sem isso, cada agente já fala com tom e velocidade próprios no navegador e no celular. Com o Kokoro, as respostas
em áudio (inclusive notas de voz no Telegram) usam vozes diferentes: Byte `pf_dora`, Bit `pm_alex`,
Kronus `pm_santa`.
```
sudo apt install -y docker.io && sudo usermod -aG docker $USER
```
Feche e abra o Ubuntu, depois:
```
sudo systemctl start docker && docker run -d --restart unless-stopped -p 8880:8880 --name kokoro ghcr.io/remsky/kokoro-fastapi-cpu:latest
```
Acrescente ao `~/.byte-env`:
```
export BYTE_VOICE_TTS_URL="http://127.0.0.1:8880/v1"
export BYTE_VOICE_TTS_MODEL="kokoro"
export BYTE_VOICE_TTS_VOICE="pf_dora"
```
e reinicie a Byte. Para mudar a voz de um agente, veja [voz.md](voz.md).

---

## Parte 2. Dia a dia

| O que fazer | Comando (no Ubuntu) |
|---|---|
| Ligar | `byte iniciar` (terminal 1) e `byte kronus` (terminal 2) |
| Desligar | Ctrl+C em cada terminal, ou `byte parar` (tudo), `byte parar principal`, `byte parar kronus` |
| Ver se está tudo bem | `byte status` |
| Backup (com a Byte desligada) | `byte backup` |

- Deixe o terminal do Ubuntu **aberto** enquanto usa: fechando todos, o Windows desliga o WSL e a Byte para.
- Reinicie (`byte parar` e `byte iniciar`) depois de: atualizar, editar o `~/.byte-env`, editar um agente.
- Toda semana: `byte backup` e copie o arquivo para o Windows:
  `cp ~/backups-byte/*.tar.gz /mnt/c/Users/SEU_USUARIO_WINDOWS/Documents/`
- Todo mês: `sudo apt update && sudo apt upgrade -y`; para atualizar o Ollama, rode de novo o instalador do passo 3;
  `ollama list` e `ollama rm NOME` para apagar modelos que não usa.

| Problema | Solução |
|---|---|
| "Ollama não respondeu" | `sudo systemctl start ollama` |
| "não começou a responder a tempo" / "timed out" | normal num notebook sem GPU quando o processador está ocupado: limpe o contexto, desligue o Kronus dedicado se não estiver usando, use `qwen2.5:1.5b`, ou aumente a espera com `export BYTE_CPU_TIMEOUT_S=600` no `~/.byte-env` |
| "Já existe uma Byte rodando" | `byte parar` |
| Celular diz "API desligada" ou não abre | o Kronus dedicado não está ligado (`byte kronus`) ou o Tailscale aponta para a porta errada (`sudo tailscale serve status` deve mostrar 8772) |
| "Os dados ... já estão em uso" | você tentou abrir o Kronus no painel principal com o dedicado ligado; use o painel dele (porta 8772) |
| Celular não abre o endereço | DNS privado do Android desativado e Tailscale ligado (passo 8) |
| Telegram não entende áudio | a Byte foi ligada sem o `~/.byte-env` com a chave do Groq |
| Um token vazou | gere outro, troque no `~/.byte-env`, reinicie (e refaça o QR, se for o do Kronus) |

---

## Parte 3. Atualizações

Atualizar nunca apaga seus dados (`~/.byte-agent`, `~/.byte-agents`), seus segredos (`~/.byte-env`) nem os seus
agentes personalizados (`~/byte-agent/agents/`).

### Caminho A: pelo GitHub (o normal)
Quando a versão nova estiver no GitHub:
```
byte parar
```
```
byte atualizar
```
Ele baixa, reinstala e roda os testes; no fim deve aparecer "passed". Depois, `byte iniciar`.

### Caminho B: por um arquivo zip
1. Baixe o `byte-agent.zip` no Windows (por exemplo, do Google Drive) para a pasta Downloads.
2. No Ubuntu:
```
byte parar
```
```
byte instalar-zip /mnt/c/Users/SEU_USUARIO_WINDOWS/Downloads/byte-agent.zip
```
Depois, `byte iniciar`.

Para mandar essa versão também para o GitHub (e o Colab pegar), no Ubuntu:
```
cd ~/byte-agent && git add -A && git commit -m "Atualização" && git push
```
(Na primeira vez o Git pede usuário e senha do GitHub: no lugar da senha, use um token criado em
github.com → Settings → Developer settings → Personal access tokens.)

### Colab
```
pip install --force-reinstall git+https://github.com/DragonClaw121/byte-agent.git
```
e rode `iniciar_byte()` de novo.

### Voltar para a versão anterior
```
cd ~/byte-agent && git log --oneline -5
```
`git checkout CÓDIGO` usa a versão escolhida; `git checkout main` volta para a mais nova.
