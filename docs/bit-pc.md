# Bit no PC (Windows) — etapa 1 (v13.47)

O Bit ganha um app para Windows: os "olhos e mãos" dele no computador. O cérebro continua na nuvem, com a mesma
conversa, memória, tarefas e e-mails do Telegram.

## Instalar e conectar
1. **Instalar (recomendado, v13.48):** abra o **PowerShell** (menu Iniciar → digite "PowerShell") e cole:
   ```
   irm https://raw.githubusercontent.com/DragonClaw121/byte-agent/main/desktop/instalar.ps1 | iex
   ```
   Ele instala o Python **oficial** (python.org, só para o seu usuário, sem administrador), as bibliotecas e o app, e
   cria o atalho **"Bit no PC"** no Menu Iniciar e na Área de Trabalho. Para atualizar, rode o mesmo comando de novo.
   *Por que não o .exe?* O Chrome e o Windows marcam como "vírus" um .exe sem assinatura digital (falso positivo comum
   em apps feitos com o PyInstaller). Pelo comando acima, nada desconhecido é baixado: o Python vem assinado pela Python
   Software Foundation, e o script confere essa assinatura antes de instalar.
   O `BitPC.exe` continua em Releases para quem preferir (pode ser bloqueado).
2. **Conectar:** no **painel do Bit** (ou da Matrix), aba **⚡ Comandos** → **🔗 Gerar link de conexão**. No
   Telegram, o equivalente é `/pc`. O link (`https://.../pc/bit#123456`) vale 10 minutos e só uma vez.
3. Cole o link no app e clique em **Conectar**.

O PC aparece em `/dispositivos`. Para desconectar: `/desparear <id>`, ou **Desconectar** no próprio app. O código de
acesso fica guardado cifrado com a sua conta do Windows (DPAPI).

## O que dá para fazer
| | |
|---|---|
| **Conversa** | A mesma do Telegram, com os comandos (`/agenda`, `/caixa`, `/triagem`...) e os botões de aprovação. |
| **📸 Olhar minha tela** | Manda UMA captura da janela em uso. Dá para escrever uma pergunta antes ("o que esse erro significa?"). |
| **● Assistente ao vivo** | Trabalhe normalmente: o Bit observa a tela, anota o que importa (quem pediu o quê, prazos, valores, tabelas) e sugere o que pode preparar. |
| **Preparar** | ✉ E-mail · ▦ Planilha · ▭ Slides · ☰ Documento · ✎ Texto: o Bit faz com o que viu. Escreva antes, na caixa, o que quer (opcional): "responder à Ana aceitando o prazo", "orçamento com os 3 itens". |
| **Avisos** | Lembretes, resumos e avisos do Bit aparecem no canto da tela. |
| **Atalhos** | **Ctrl+Alt+B** abre o Bit. **Ctrl+Alt+L** liga ou desliga a ajuda ao vivo. Funcionam com o app minimizado. |

### Modo compacto (barra flutuante, v1.1 do app)
O botão **▭ Modo compacto**, no topo da janela, transforma o Bit numa barrinha que fica sempre por cima, como os
miniplayers do Spotify e do YouTube. Arraste pela alça (⋮) para onde quiser: a posição fica guardada. O app abre no
último modo usado.

| Ícone | Faz |
|---|---|
| ✎ | Abre o campo de pergunta. A resposta aparece num balão colado na barra, com os botões de aprovação. **Ctrl+Alt+B** também abre o campo. |
| ◉ | Olhar minha tela (se houver texto no campo, vai como pergunta). |
| ● / ■ | Liga ou para a ajuda ao vivo. O passo atual aparece na própria barra. |
| ♪ | Liga ou desliga a orientação por voz. |
| ! (com número) | Avisos não lidos. Clique para ver. |
| ⤢ | Volta para a janela completa. |
| ✕ | Fecha o Bit. |

No Windows, os ícones são os do próprio sistema (Segoe MDL2). A tabela mostra os símbolos de reserva.

### Assistente ao vivo: observa e deixa o trabalho pronto (v1.2 do app)
- **Ligar:** botão **● Assistente ao vivo** (ou **Ctrl+Alt+L**). Aparece o aviso vermelho "Bit observando" no topo.
- **Enquanto você trabalha:** o app olha a tela a cada 2 s, no próprio PC, mas só manda um quadro quando ela **muda**
  (no máximo 1 a cada 6 s). Com a tela parada, não manda nada nem gasta nada. A cada quadro, o Bit anota os fatos novos
  e mostra **sugestões** do que pode preparar: "Responder à Ana confirmando o prazo", "Planilha do orçamento".
- **Preparar:** clique numa sugestão ou num dos botões **Preparar**. Com o assistente desligado, os botões usam só a
  tela atual.
  - **E-mail e texto:** abrem numa janela para revisar, com **Copiar o texto**, **Copiar o assunto** e **Abrir no
    e-mail**. "Abrir no e-mail" cria um e-mail novo no Outlook (ou no seu programa padrão) já com destinatário, assunto
    e texto. O texto também fica copiado, caso seja longo demais para entrar inteiro.
  - **⌕ Cliente (v1.3):** pesquisa o cliente que está na tela (cadastro pelo CNPJ, web e as compras que aparecem) e
    entrega um plano de conta (.docx) e um e-mail de abordagem. "Pesquise esse cliente", na conversa, faz o mesmo.
    Veja [clientes.md](clientes.md).
  - **Planilha (.xlsx), apresentação (.pptx) e documento (.docx):** são criados de verdade, salvos em
    **Documentos\Bit** e abertos sozinhos.
- **Perguntas:** com o assistente ligado, escreva na caixa ("onde fica a opção de filtro?"). A pergunta vai junto com a
  tela; se for "onde clicar", ele marca o lugar com um retângulo vermelho.
- **Desligar:** Ctrl+Alt+L, clique no aviso vermelho, ou ele desliga sozinho depois de 20 minutos sem mudança na tela.
  As notas da sessão ficam na conversa (o Bit lembra o contexto se você pedir algo depois).

### Por que travava (corrigido na v1.2)
- **Captura:** usava o modo do Windows que recompõe a tela inteira a cada foto (CAPTUREBLT) e fazia o sistema
  "engasgar" por instantes. Agora está desligado.
- **Foco:** os avisos e o balão pegavam o foco ao aparecer, e quem estava digitando no Outlook perdia a digitação.
  Agora eles aparecem sem tirar o foco de ninguém.
- **Retângulo vermelho:** o "deixar o clique passar" era aplicado na janela errada, então ele podia bloquear cliques
  por alguns segundos. Agora fica certo.
- **Ritmo:** o app olha a tela com metade da frequência de antes.

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
