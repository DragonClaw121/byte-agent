# Bit no PC (Windows) — etapa 1 (v13.47)

O Bit ganha um app para Windows: os "olhos e mãos" dele no computador. O cérebro continua na nuvem, com a mesma
conversa, memória, tarefas e e-mails do Telegram.

## Instalar e conectar
1. Baixe o app: https://github.com/DragonClaw121/byte-agent/releases/download/app-bit-pc/BitPC.exe
   O app não é assinado. Na primeira vez, o Windows pode mostrar "O Windows protegeu o computador": clique em
   **Mais informações → Executar assim mesmo**. Em PC de empresa, a TI pode bloquear: peça a liberação.
2. No Telegram, mande **`/pc`** para o Bit. Ele responde com um link `https://.../pc/bit#123456`, que vale 10
   minutos e só uma vez.
3. Cole o link no app e clique em **Conectar**.

O PC aparece em `/dispositivos`. Para desconectar: `/desparear <id>`, ou **Desconectar** no próprio app. O código de
acesso fica guardado cifrado com a sua conta do Windows (DPAPI).

## O que dá para fazer
| | |
|---|---|
| **Conversa** | A mesma do Telegram, com os comandos (`/agenda`, `/caixa`, `/triagem`...) e os botões de aprovação. |
| **📸 Olhar minha tela** | Manda UMA captura da janela em uso. Dá para escrever uma pergunta antes ("o que esse erro significa?"). |
| **🔴 Ajuda ao vivo** | Diga o objetivo ("montar uma tabela dinâmica", "ajustar esse slide no Canva") e trabalhe normalmente. O Bit acompanha a tela e orienta passo a passo. |
| **Avisos** | Lembretes, resumos e avisos do Bit aparecem no canto da tela. |
| **Atalhos** | **Ctrl+Alt+B** abre o Bit. **Ctrl+Alt+L** liga ou desliga a ajuda ao vivo. Funcionam com o app minimizado. |

### Como a ajuda ao vivo funciona
- O app olha a tela a cada segundo, no próprio PC, mas **só manda um quadro quando a tela muda de verdade**: um menu
  abriu, você clicou, digitou algo. No máximo um quadro a cada 2,5 s. Com a tela parada, não manda nada.
- **Perguntas durante a ajuda:** escreva na caixa da conversa ("e agora, onde fica a opção de filtro?"). A pergunta
  vai junto com a tela atual.
- **Orientação:** você escolhe nas opções do app. **Texto** mostra uma janelinha no canto, com o passo atual. **Voz**
  usa a voz do Windows em português. Pode ligar uma, as duas ou nenhuma.
- **"Clique aqui":** quando dá, o Bit desenha um retângulo vermelho em volta do botão certo. O clique passa direto
  por ele.
- **Quando para:** com Ctrl+Alt+L, clicando no aviso vermelho do topo, quando o objetivo parece concluído, ou depois
  de 3 minutos sem mudança na tela.
- **No fim:** o Bit pergunta se deve **guardar o passo a passo como um processo** na memória. Nas próximas vezes, ele
  já conhece o caminho. Essa é a base da etapa 3 (automação).

## Privacidade
- **Só com o aviso na tela:** só olha a tela quando você pede ("Olhar minha tela") ou com a ajuda ao vivo ligada, e
  aí aparece o aviso vermelho no topo.
- **O próprio Bit fica fora:** as janelas do Bit não aparecem nas capturas, graças a um recurso do Windows 10/11.
- **Janelas que ele nunca olha:** as que têm senha, banco, gerenciadores de senha ou navegação anônima/InPrivate. A
  ajuda ao vivo fica em pausa enquanto elas estiverem em uso.
- **Nada da tela é gravado** em disco, nem no PC nem na nuvem. A imagem existe só durante o pedido. O que fica é o
  texto: as respostas e, se você quiser, o passo a passo.
- **Texto na tela** (e-mails, páginas, documentos) é tratado como conteúdo de terceiros: o Bit nunca segue
  instruções escritas nele.

## Custos e limites
- **Uso normal:** cada captura vai reduzida (até 1280 px de largura). Uma sessão de ajuda de 20 minutos custa poucos
  centavos a ~R$2, conforme quanto a tela muda.
- **Limite diário:** 1500 capturas por dia por agente (`BYTE_PC_QUADROS_DIA`), para proteger a cota.
- **Modelo:** precisa de um modelo da Azure **com visão**. Se a sua implantação não aceitar imagens, o Bit avisa.
  Nesse caso, crie uma implantação com visão (ex.: gpt-4.1-mini) e defina `BYTE_AZURE_VISAO=<nome da implantação>` no
  contêiner.

## Próximas etapas
- **Etapa 2 — observação do dia:** linha do tempo de programas e janelas, diário de trabalho e processos repetidos
  encontrados sozinhos. Só com o modo ligado.
- **Etapa 3 — automação assistida:** os processos guardados viram receitas que o Bit executa com a sua aprovação.
