# Byte 24/7 na Azure com Container Apps (sem máquina virtual)

Para quando os tamanhos pequenos de VM não estão disponíveis na assinatura. O contêiner liga sozinho, religa se cair,
guarda os dados no Azure Files e abre a página de voz do Kronus num endereço HTTPS da própria Azure.

| Peça | Para quê | Custo estimado |
|---|---|---|
| Conta de armazenamento + compartilhamento `byte-dados` | código da Byte (zip) e cópia dos dados | centavos por mês |
| Ambiente + aplicativo de contêiner `byte` (0,25 vCPU, 0,5 GiB, 1 réplica) | roda Matrix, Byte, Bit e Kronus | ~US$ 5 a 15 por mês |
| Azure OpenAI (já criado) | o modelo | poucos dólares por mês |

Como funciona: o contêiner usa a imagem pública `python:3.12-slim`, instala a Byte a partir do zip guardado no
armazenamento e entrega para o supervisor (`byte_agent.nuvem`). O banco fica no disco do contêiner (o SQLite não
funciona direto num compartilhamento de rede) e o supervisor salva uma cópia no armazenamento a cada 5 minutos e ao
desligar; ao ligar, restaura a última cópia. Em uma queda brusca, perde-se no máximo uns 5 minutos de conversa.

**O que não existe no contêiner:** o painel web (por segurança ele só abre "de dentro" da máquina). Use o Telegram
(`/agente`, `/modelo`, `/status`, `/agenda`...) e a página de voz. A live da Byte (Twitch + VTube Studio) continua no PC.

> **Importante:** o portal da Azure passou a criar ambientes do tipo **"Express"**, que **não aceitam
> armazenamento** (erro "'Storage' resources are not supported on express environments"). Por isso, depois do
> passo 2 (armazenamento), crie ambiente e aplicativo pelo **Cloud Shell** com `scripts/azure-conteiner.sh`: ele
> apaga ambientes Express do grupo, cria o ambiente de perfis de carga de trabalho, liga o armazenamento e cria o
> aplicativo completo (segredos, variáveis, volume, 1 réplica). Os passos 3 a 5 abaixo ficam só como referência.
>
> No portal: ícone **Cloud Shell** (`>_`) no topo → **Bash** → **Gerenciar arquivos → Carregar** →
> `azure-conteiner.sh` → `bash azure-conteiner.sh`.

---

## 1. Preparar os arquivos (no notebook)
1. Baixe do Google Drive o `byte-agent-v13.4.zip` e o `conteiner-iniciar.sh`.
2. Leve seus dados (memórias, agenda, conversas) — no Ubuntu:
   ```
   byte parar
   byte backup
   cp ~/backups-byte/*.tar.gz /mnt/c/Users/SEU_USUARIO_WINDOWS/Downloads/
   ```
3. **Não abra nem edite o `conteiner-iniciar.sh` no Bloco de Notas** (ele troca as quebras de linha e o script deixa
   de funcionar no Linux).

## 2. Conta de armazenamento
Portal → **Contas de armazenamento** → **+ Criar**:

| Aba | Campo | Valor |
|---|---|---|
| Básico | Grupo de recursos | `byte-rg` |
| | Nome | único, só minúsculas e números, ex.: `bytedadosgabriel` |
| | Região | Brazil South (ou outra da sua lista permitida) |
| | Serviço primário | **Arquivos do Azure** (se perguntar) |
| | Desempenho | **Standard** |
| | Redundância | **LRS** (armazenamento com redundância local; o mais barato) |
| Avançado | Exigir transferência segura | marcado (padrão) |
| | **Permitir acesso à chave da conta de armazenamento** | **marcado** (o contêiner usa a chave) |
| | Camada de acesso | Frequente (Hot) |
| Rede | Acesso de rede | **Habilitar o acesso público de todas as redes** |
| Proteção de dados | | padrão |
| Criptografia | | padrão (chaves gerenciadas pela Microsoft) |
| Marcas | `projeto` | `byte` |

**Examinar + criar** → **Criar** → **Ir para o recurso**.

**Compartilhamento:** no recurso, **Armazenamento de dados → Compartilhamentos de arquivos → + Compartilhamento de
arquivos**: nome **`byte-dados`**, camada **Transação otimizada** (ou "Hot"), **Criar**.

**Enviar os arquivos:** abra o `byte-dados` → **Carregar** → selecione:
- `byte-agent-v13.4.zip`
- `conteiner-iniciar.sh`
- o backup `byte-AAAA-MM-DD_HHMM.tar.gz` (seus dados)
- opcional: crie a pasta **`agents`** (**+ Adicionar diretório**) e envie seus manifestos personalizados.

**Chave:** no recurso, **Segurança + rede → Chaves de acesso** → **Mostrar** → copie a **key1** (vai ser usada no
passo 4; trate como senha).

## 3. Aplicativo de contêiner
Portal → **Aplicativos de Contêiner** → **+ Criar** → **Aplicativo de Contêiner**:

**Básico**
| Campo | Valor |
|---|---|
| Grupo de recursos | `byte-rg` |
| Nome do aplicativo de contêiner | `byte` |
| Origem da implantação | Imagem de contêiner |
| Região | a mesma do armazenamento (se não aparecer, outra da lista permitida) |
| Ambiente de Aplicativos de Contêiner | **Criar novo** → nome `byte-ambiente`; tipo/plano **Consumo**; aba **Monitoramento**: destino dos logs **Azure Log Analytics** (sem logs a Azure cria o ambiente como "Express", que não aceita armazenamento); aba **Rede**: pública, sem rede virtual → **Criar** |

**Contêiner**
| Campo | Valor |
|---|---|
| Usar imagem de início rápido | **desmarcado** |
| Nome | `byte` |
| Origem da imagem | **Docker Hub ou outros registros** |
| Tipo de imagem | **Público** |
| Servidor de logon do registro | `docker.io` |
| Imagem e marca | **`python:3.12-slim`** |
| Comando de substituição | **`/bin/sh`** |
| Argumentos de substituição | **`/mnt/dados/conteiner-iniciar.sh`** |
| CPU e memória | **0,25 núcleo de CPU, 0,5 Gi de memória** |
| Variáveis de ambiente | deixe para o passo 5 |

**Entrada (Ingress)**
| Campo | Valor |
|---|---|
| Entrada | **Habilitado** |
| Tráfego de entrada | **Aceitando o tráfego de qualquer lugar** |
| Tipo de entrada | HTTP |
| Transporte | Automático |
| Conexões não seguras | **não** permitir |
| Porta de destino | **`8772`** (a página de voz e a API do Kronus; o resto fica fechado) |

**Marcas:** `projeto` = `byte`. **Examinar + criar** → **Criar**. É normal ele ficar reiniciando até o passo 4: ainda
não enxerga os arquivos.

## 4. Ligar o armazenamento ao contêiner
1. Abra o **ambiente** `byte-ambiente` → **Configurações → Arquivos do Azure** (Azure Files) → **+ Adicionar**:
   nome **`dados`**, protocolo **SMB**, nome da conta de armazenamento, **chave** (key1 do passo 2), compartilhamento
   **`byte-dados`**, acesso **Leitura/gravação** → **Salvar**.
2. Abra o **aplicativo** `byte` → **Aplicativo → Revisões e réplicas** → **Criar nova revisão** (ou **Contêineres →
   Editar e implantar**):
   - aba **Volumes** → **+ Adicionar**: tipo **Volume do Azure Files**, nome **`dados`**, armazenamento **`dados`** →
     **Adicionar**;
   - aba **Contêiner** → clique no contêiner `byte` → **Montagens de volume** → **+ Adicionar**: volume **`dados`**,
     caminho **`/mnt/dados`** → **Salvar**;
   - ainda não clique em Criar: siga para o passo 5 na mesma revisão (ou crie agora e edite de novo depois).

## 5. Segredos e variáveis
1. No aplicativo `byte` → **Configurações → Segredos** → **+ Adicionar** (um por vez, tipo "Segredo do Aplicativo de
   Contêiner"):

| Nome do segredo | Valor |
|---|---|
| `azure-key` | chave da Azure OpenAI |
| `telegram-token` | token do bot (BotFather) |
| `groq-key` | chave do Groq |
| `tavily-key` | chave do Tavily |
| `kronus-token` | o KRONUS_API_TOKEN (o mesmo do seu `~/.byte-env`, para o celular continuar funcionando) |

2. **Editar e implantar** → contêiner `byte` → **Variáveis de ambiente**:

| Nome | Origem | Valor |
|---|---|---|
| `BYTE_MODEL` | Manual | `azure:byte-mini` |
| `AZURE_OPENAI_ENDPOINT` | Manual | `https://SEU-RECURSO.openai.azure.com` |
| `AZURE_OPENAI_API_KEY` | Referência a segredo | `azure-key` |
| `BYTE_TELEGRAM_TOKEN` | Referência a segredo | `telegram-token` |
| `BYTE_TELEGRAM_CHAT_IDS` | Manual | o seu chat_id |
| `BYTE_STT_URL` | Manual | `https://api.groq.com/openai/v1` |
| `BYTE_STT_MODEL` | Manual | `whisper-large-v3-turbo` |
| `BYTE_STT_API_KEY` | Referência a segredo | `groq-key` |
| `TAVILY_API_KEY` | Referência a segredo | `tavily-key` |
| `KRONUS_API_TOKEN` | Referência a segredo | `kronus-token` |
| `BYTE_AUTONOMIA` | Manual | `1` |

3. **Escala** (aba da mesma revisão, ou **Aplicativo → Escala**): **mínimo 1 e máximo 1 réplica**. Mínimo 1 mantém
   ligado (Telegram e lembretes); máximo 1 é obrigatório: duas cópias brigariam pelo bot e pelos dados.
4. **Criar** (a revisão nova).

**Antes de a revisão subir, desligue a Byte do notebook** (`byte parar`): as duas com o mesmo bot do Telegram brigam
pelas mensagens.

## 6. Conferir
- Aplicativo → **Monitoramento → Fluxo de log**: deve aparecer `[NUVEM] Instalando byte-agent-v13.4.zip...`,
  `[NUVEM] Dados restaurados...`, `[NUVEM] Ligando principal...`, `[NUVEM] Ligando kronus...` e
  `[BYTE] Telegram ativo para os chats...`. A instalação leva de 1 a 2 minutos a cada partida.
- Mande uma mensagem ao bot no Telegram.
- No compartilhamento `byte-dados`, depois de uns 5 minutos aparece o `byte-dados.tar.gz` (a cópia dos dados).

## 7. Kronus no celular
No aplicativo, **Visão geral → URL do Aplicativo** (algo como `https://byte.xxxx.brazilsouth.azurecontainerapps.io`).
No Ubuntu do notebook, gere o QR:
```
source ~/.byte-env && qrencode -t ansiutf8 "https://ENDERECO-DO-APP/voz#token=$KRONUS_API_TOKEN"
```
Leia com o celular e adicione à tela inicial. Não precisa de Tailscale: o endereço é HTTPS público, e só quem tem o
token conversa com o Kronus. (No notebook, dá para desligar o `tailscale serve` antigo:
`sudo tailscale serve --https=443 off`.)

---

## Atualizar a Byte
1. Envie o zip novo ao compartilhamento `byte-dados` (pode apagar o antigo; ele usa sempre o mais novo).
2. Aplicativo → **Revisões e réplicas** → revisão ativa → **Reiniciar**.
Os dados continuam: ele salva ao desligar e restaura ao ligar.

## Problemas comuns
| Sintoma no Fluxo de log | Solução |
|---|---|
| `Nenhum byte-agent*.zip em /mnt/dados` | volume não montado em `/mnt/dados` (passo 4) ou o zip não foi enviado |
| `/mnt/dados/conteiner-iniciar.sh: not found` ou `\r` | o script não foi enviado, ou foi editado no Windows: envie de novo o original |
| `Defina BYTE_MODEL` | faltou a variável do passo 5 |
| reinícios com "OOMKilled" | aumente para 0,5 vCPU / 1 Gi (custa o dobro) |
| `HTTP 401` da Azure | segredo `azure-key` errado |

## Custos e cuidados
- Mantenha **1 réplica**; mais que isso multiplica o custo e quebra o Telegram.
- O orçamento de US$ 15 com alerta em 80% (Gerenciamento de Custos) continua valendo.
- Para pausar tudo sem apagar: aplicativo → **Desativar** (fica sem custo de processamento; os dados ficam no
  armazenamento).

## Painel pela internet (v13.6)
Com `BYTE_PAINEL_REMOTO=1` (o script já liga), o endereço HTTPS do contêiner também abre os painéis, **só com o
token** de cada um. Um roteador na porta pública manda o pedido para o painel dono do token; sem token, tudo continua
indo para o Kronus (página `/voz` e API). Os tokens ficam fixos em `painel-tokens.json` no compartilhamento, então o
favorito não muda a cada reinício.

Para ver os endereços:
```
az containerapp logs show -g byte-rg -n byte --tail 300 | grep -o 'Painel [^"]*'
```
Trate esses links como senha: quem tiver o link controla a Byte. Para trocar os tokens, apague
`painel-tokens.json` do compartilhamento e reinicie. Para desligar o painel remoto:
`az containerapp update -g byte-rg -n byte --set-env-vars BYTE_PAINEL_REMOTO=0`.
