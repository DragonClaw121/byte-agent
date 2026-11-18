# Byte online na Azure (assinatura Azure for Students)

Resultado: os agentes ligados 24 horas por dia num servidor pequeno da Azure, com o "cérebro" no Azure OpenAI
(rápido, com ferramentas de verdade), Telegram, página de voz do Kronus no celular e o notebook livre.

Custo estimado: US$ 5 a 15 por mês (o crédito de US$ 100 dura de 8 a 12 meses). Quando o crédito acaba, os serviços
param; não há cobrança no cartão. Valores são estimativas: confira na calculadora de preços da Azure.

| Etapa | Onde | Tempo |
|---|---|---|
| 1. Recurso do Foundry (Azure OpenAI) | portal da Azure | 5 min |
| 2. Implantar o modelo | portal do Foundry | 5 min |
| 3. Testar no notebook | Ubuntu do notebook | 5 min |
| 4. Alerta de orçamento | portal da Azure | 3 min |
| 5. Criar a máquina virtual | portal da Azure | 10 min |
| 6. Instalar a Byte no servidor | terminal (SSH) | 15 min |
| 7. Tailscale, celular e painel | servidor e notebook | 10 min |

---

## 1. Recurso do Foundry (Azure OpenAI)
Portal da Azure → **Microsoft Foundry** → **Criar**:

| Campo | O que colocar |
|---|---|
| Assinatura | Azure for Students |
| Grupo de recursos | **Criar novo** → `byte-rg` (todos os recursos da Byte ficam juntos; apagar o grupo apaga tudo) |
| Nome | único no mundo, só letras, números e hífen, ex.: `byte-ia-SEUNOME` (vira o endereço `https://byte-ia-SEUNOME...`) |
| Região | **East US 2** (é onde há mais modelos). Brazil South funciona se o modelo escolhido aparecer lá. |
| Application insights | **None** (evita custo extra) |

Abas **Armazenamento**, **Identity**, **Criptografia** e **Tags**: deixe como estão. Em **Inbound Networking**, deixe o
acesso por **todas as redes** (o servidor chama a API pela internet, protegido pela chave). **Examinar + criar** →
**Criar**. Leva de 1 a 3 minutos.

## 2. Implantar o modelo
1. Abra o recurso criado → **Ir para o portal do Foundry** (ai.azure.com).
2. Menu **Modelos + pontos de extremidade** (ou **Implantações**) → **+ Implantar modelo** → **Implantar modelo base**.
3. Escolha um modelo "mini" com suporte a ferramentas: **gpt-4.1-mini** ou **gpt-4o-mini** (ou o mini mais novo da
   lista). Evite os de raciocínio (família "o" e similares): funcionam, mas são mais lentos e caros para conversa.
4. **Nome da implantação: `byte-mini`** (é esse nome que a Byte usa, não o nome do modelo).
5. Tipo de implantação: **Global Standard**. Em **Personalizar**, reduza o limite de tokens por minuto para **30K**:
   isso limita o gasto se algo sair do controle.
6. **Implantar**.

**Outros modelos (Nemotron, Llama, Mistral, DeepSeek, Grok, Phi...)** também funcionam, desde que:
- a implantação seja **paga por token** (Global Standard / Serverless / "vendido diretamente pela Azure"). Evite
  **Managed compute** (computação gerenciada): ele aluga uma GPU por hora e consome o crédito em poucos dias;
- o modelo seja cobrado do seu crédito: modelos de parceiros vendidos pelo Marketplace muitas vezes não podem ser
  contratados em assinaturas de estudante;
- de preferência, o cartão do modelo mostre suporte a **ferramentas** ("tool calling" / "function calling"). Sem isso
  a Byte usa o modo texto: conversa normalmente, mas agenda, lembretes e busca ficam menos confiáveis.
A Byte descobre sozinha o endereço certo da API para cada modelo. Para comparar, implante dois modelos e rode no
notebook: `python -m byte_agent avaliar --modelos azure:byte-mini,azure:NOME-DA-OUTRA-IMPLANTACAO`.

**Endereço e chave:** portal da Azure → seu recurso → **Gerenciamento de Recursos → Chaves e Ponto de Extremidade**.
Copie a **Chave 1** e o **Ponto de extremidade** (`https://byte-ia-SEUNOME.openai.azure.com/` ou
`...cognitiveservices.azure.com/`). Trate a chave como senha: nunca no chat, no GitHub ou em print.

## 2b. Vários modelos (um por agente, ou troca na hora)
Implante quantos modelos quiser, cada um com um nome (ex.: `byte-mini` e `nemotron`). Depois:
- **Modelo base** (Matrix e agentes sem modelo próprio): `BYTE_MODEL="azure:byte-mini"` no `~/.byte-env`.
- **Trocar na hora:** no Telegram, `/modelo azure:nemotron` (`/modelo` sozinho mostra o atual). A escolha vira o
  novo modelo base até reiniciar.
- **Modelo próprio de um agente:** copie o manifesto para `agents/` e preencha `"modelo"`. Exemplo para o Bit:
  ```
  mkdir -p ~/byte-agent/agents && cp ~/byte-agent/byte_agent/agentes/bit.json ~/byte-agent/agents/bit.json
  nano ~/byte-agent/agents/bit.json          # "modelo": "azure:nemotron"
  ```
  Ao trocar para o Bit, ele usa o Nemotron; ao voltar para a Matrix, volta o modelo base. No servidor, a pasta é a
  mesma (`~/byte-agent/agents/`) e vale também para o Kronus dedicado.

## 3. Testar no notebook primeiro
No Ubuntu do notebook, acrescente ao `~/.byte-env`:
```
export BYTE_MODEL="azure:byte-mini"
export AZURE_OPENAI_ENDPOINT="https://byte-ia-SEUNOME.openai.azure.com"
export AZURE_OPENAI_API_KEY="SUA_CHAVE_1"
```
`byte parar` e `byte iniciar`. As respostas passam a chegar em segundos, e o painel mostra **☁️ Azure**. Para voltar
ao Ollama local, troque `BYTE_MODEL` para `qwen2.5:3b`.

## 4. Alerta de orçamento
Portal da Azure → **Gerenciamento de Custos** → **Orçamentos** → **Adicionar**: escopo `Azure for Students`, valor
**US$ 15 por mês**, alerta por e-mail em **80%**. Você é avisado antes de gastar demais.

> **Sem tamanho de VM barato disponível na sua assinatura?** Use Container Apps: [azure-conteiner.md](azure-conteiner.md).

## 5. Criar a máquina virtual
Portal → **Máquinas virtuais** → **Criar → Máquina virtual do Azure**:

| Campo | O que colocar |
|---|---|
| Grupo de recursos | `byte-rg` |
| Nome | `byte-servidor` |
| Região | a mesma do passo 1 (ou Brazil South, mais perto de você) |
| Opções de disponibilidade | Nenhuma redundância de infraestrutura necessária |
| Imagem | **Ubuntu Server 24.04 LTS - x64 Gen2** |
| Tamanho | um marcado como **"elegível para serviços gratuitos"** (ex.: **Standard_B1s**, ou B2ats_v2). Se nenhum aparecer, **Standard_B1ms**. Não precisa de GPU. |
| Autenticação | **Chave pública SSH**, usuário `byte`, **Gerar novo par de chaves** (baixe o arquivo `.pem` no fim) |
| Portas de entrada públicas | **SSH (22)** (depois do Tailscale dá para fechar) |
| Discos | SSD padrão; 30 GB bastam |
| Gerenciamento | **Desligamento automático: desativado** (a ideia é ficar ligada) |

**Examinar + criar** → **Criar** → **Baixar chave privada e criar recurso**. Anote o **IP público** da VM.

## 6. Instalar a Byte no servidor
No Ubuntu do notebook (troque o caminho do `.pem` e o IP):
```
mkdir -p ~/.ssh && cp /mnt/c/Users/SEU_USUARIO_WINDOWS/Downloads/byte-servidor_key.pem ~/.ssh/byte.pem && chmod 600 ~/.ssh/byte.pem
```
Envie o código e os seus segredos do notebook para o servidor:
```
scp -i ~/.ssh/byte.pem /mnt/c/Users/SEU_USUARIO_WINDOWS/Downloads/byte-agent-v13.0.zip ~/.byte-env byte@IP_DA_VM:~
```
Entre no servidor:
```
ssh -i ~/.ssh/byte.pem byte@IP_DA_VM
```
Já no servidor:
```
sudo apt-get update -qq && sudo apt-get install -y unzip && unzip -o byte-agent-v13.0.zip
```
```
bash ~/byte-agent/scripts/instalar-servidor.sh
```
O `~/.byte-env` que você enviou já tem Telegram, Groq, Tavily e o token do Kronus. Confira que ele tem as três linhas da
Azure (passo 3) e acrescente `export BYTE_AUTONOMIA=1` para ligar as mensagens por iniciativa própria:
```
nano ~/.byte-env
```
**Antes de ligar no servidor, desligue a Byte do notebook** (`byte parar` no notebook): os dois com o mesmo bot do
Telegram brigam pelas mensagens. Depois, no servidor:
```
sudo systemctl restart byte byte-kronus
```
```
byte status
```
`byte logs` mostra o registro ao vivo (Ctrl+C sai). A partir daqui a Byte liga sozinha quando a VM liga e volta
sozinha se cair.

## 7. Tailscale, celular e painel
No servidor:
```
sudo tailscale up --ssh
```
(abra o link e entre na mesma conta do celular). Depois:
```
sudo tailscale serve --bg 8772
```
- **Celular:** o endereço muda para o do servidor: `https://byte-servidor.SEU-TAILNET.ts.net/voz`. Gere o QR no
  servidor: `source ~/.byte-env && qrencode -t ansiutf8 "https://byte-servidor.SEU-TAILNET.ts.net/voz#token=$KRONUS_API_TOKEN"`.
- **Painel:** ele só abre "de dentro" do servidor, por segurança. Do notebook, abra um túnel e deixe o terminal aberto:
  ```
  ssh -i ~/.ssh/byte.pem -L 9765:127.0.0.1:8765 -L 9772:127.0.0.1:8772 byte@IP_DA_VM
  ```
  e acesse `http://127.0.0.1:9765/ui?token=SEU_BYTE_UI_TOKEN` (Kronus: porta 9772).
- **Mais seguro (opcional):** com o Tailscale funcionando, use `byte-servidor` no lugar do IP nos comandos `ssh`
  e remova a regra de SSH público: portal → VM → **Rede** → apague a regra de entrada da porta 22.

---

## Dia a dia no servidor
| O que fazer | Comando (no servidor) |
|---|---|
| Ver estado | `byte status` |
| Registro ao vivo | `byte logs` ou `byte logs kronus` |
| Reiniciar | `byte reiniciar` |
| Desligar / ligar | `byte parar` / `byte iniciar` e `byte kronus` |
| Atualizar (zip novo) | envie com `scp`, depois `byte parar`, `byte instalar-zip ~/byte-agent-vX.zip`, `byte reiniciar` |
| Backup | `byte parar`, `byte backup`, `byte reiniciar` |

Para economizar quando não for usar por dias: portal → VM → **Parar** (a VM parada não cobra processamento; o disco
continua cobrando alguns centavos).

## Quanto custa usar
- **Modelo:** uma mensagem envia uns 4 a 5 mil tokens (personalidade, ferramentas, memórias e histórico). Com um
  modelo mini, isso dá frações de centavo por mensagem: algumas dezenas por dia ficam em poucos dólares por mês.
- **Servidor:** gratuito nos tamanhos "elegíveis para serviços gratuitos" (enquanto valer o benefício); senão, por
  volta de US$ 8 a 15 por mês. O IP público custa alguns dólares por mês.
- O painel mostra o uso de tokens da Azure na aba de conexão (`azure.usage`).
