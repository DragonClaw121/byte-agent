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

   **Conferir que o .exe é o original (v1.18):** cada versão sai com três comprovantes, todos gratuitos.
   - **Carregador próprio:** o carregador do PyInstaller é compilado no próprio CI. O carregador pronto é o mesmo usado
     por muitos vírus, e é ele que os antivírus costumam marcar.
   - **Informações de versão:** o .exe traz produto, descrição e versão em Propriedades → Detalhes.
   - **SHA-256:** vai em `BitPC.exe.sha256` e nas notas da versão. No PowerShell, compare com:
     ```
     Get-FileHash BitPC.exe
     ```
   - **Atestado de origem do GitHub:** prova que o arquivo foi gerado pelo CI deste repositório, a partir do commit
     indicado. Para conferir:
     ```
     gh attestation verify BitPC.exe --repo DragonClaw121/byte-agent
     ```
   - **Link do VirusTotal** pelo hash. Se ninguém tiver enviado o arquivo ainda, dá para enviar lá.

   Nada disso substitui uma assinatura digital: o Windows e alguns antivírus ainda podem avisar.
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

### Modelos de e-mail e assinatura (v1.4 do app)
- Em **Preparar → ☰ Meus modelos de e-mail e assinatura** (também na barra compacta):
  - crie, edite e apague modelos, cada um com nome, "quando usar", assunto e texto. Use `{{nome}}`, `{{pedido}}`...
    onde muda;
  - defina a sua **assinatura**.
- **Importar do Outlook:** lê os modelos que o Outlook clássico guarda no PC (arquivos `.oft` em
  `%APPDATA%\Microsoft\Templates`) e a sua assinatura (`%APPDATA%\Microsoft\Signatures`). No Outlook novo, os modelos
  ficam na nuvem: abra cada um, copie e cole.
- **Salvar como modelo:** num e-mail que o Bit preparou e você ajustou, clique em **Salvar como modelo**.
- **Uso automático:** ao preparar um e-mail, o Bit escolhe o modelo que combina com a situação, mantém o seu texto e
  tom, preenche o que muda e assina. A janela mostra "Baseado no seu modelo …". Sem modelo adequado, ele escreve no seu
  estilo (saudações e despedidas dos seus modelos).

### Modelos com o FORMATO do Outlook (v1.5 do app, v13.52)
Modelos feitos em HTML no Outlook (tabelas, cores, fontes, logo, banner) agora são guardados com o formato, e o e-mail
pronto sai igual ao modelo.
- **Colar do Outlook (com formato):** no Outlook, abra o modelo, clique no corpo, `Ctrl+A` e `Ctrl+C`; no Bit, em
  **☰ Meus modelos**, clique em **Novo**, depois em **Colar do Outlook (com formato)**, dê um nome e salve. O ◆ na lista
  marca os modelos com formato. Desde a v1.6, `Ctrl+V` no campo do texto faz o mesmo.
- **Importar do Outlook:** os `.oft` da pasta de modelos são abertos pelo próprio Outlook clássico, que entrega o HTML
  completo e as imagens internas. Sem o Outlook clássico (só o Outlook novo), o Bit lê o arquivo direto: o HTML que fica
  dentro do RTF comprimido do `.oft` e as imagens anexadas (v1.6). Se algum vier só com o texto, a janela diz qual.
  A assinatura também vem com o formato e a imagem (o arquivo `.htm` da pasta Signatures).
- **Importar arquivo…:** escolha `.oft`, `.msg`, `.eml` ou `.htm`/`.html` (por exemplo, um e-mail salvo com
  **Arquivo → Salvar como**).
- **Campos:** `{{nome}}` ou, como no Outlook, `[Nome]`, `[SOFTWARE]` (entre colchetes, começando por maiúscula).
- **Se o app avisar "servidor na versão antiga"**, o formato foi descartado: a nuvem procura versões novas a cada 6 h.
  Mande `/atualizar` para o Bit e importe de novo.
- **Use `{{nome}}`, `{{pedido}}`… no modelo do Outlook**, antes de copiar: o Bit troca só esses pontos e o visual fica
  idêntico. Sem campos, a IA troca os textos e o Bit confere se a estrutura (tabelas e imagens) continuou inteira; se
  não continuou, ou se o modelo for muito grande, o e-mail sai só em texto e a janela avisa.
- **No e-mail pronto:**
  - **Copiar formatado:** no Outlook, clique no corpo do e-mail e `Ctrl+V`;
  - **Abrir no Outlook:** abre um e-mail novo já formatado, com destinatário e assunto, para você revisar e enviar:
    - **Outlook novo (v1.7):** o Bit abre o e-mail novo pelo próprio Outlook novo (o "app de e-mail padrão" do
      Windows) e cola o corpo com o formato e as imagens no campo da mensagem — só no e-mail com o mesmo assunto,
      achado pela acessibilidade do Windows. Não mexa no teclado por alguns segundos. Se não der para colar sozinho, o
      corpo formatado fica copiado: clique no corpo e `Ctrl+V`;
    - **Outlook clássico:** pelo próprio Outlook (automação COM);
    - o Bit escolhe pelo Outlook que está aberto; para fixar, clique em **Outlook: novo/clássico (trocar)** na janela;
    - se o e-mail abrir no Outlook errado: Configurações do Windows → Apps → Apps padrão → Outlook (new) → `MAILTO`;
    - se não colar sozinho, a janela diz o motivo e um aviso lembra o `Ctrl+V` (o e-mail formatado fica copiado). O
      diagnóstico, sem o texto do e-mail, fica em `%APPDATA%\BitPC\outlook-novo.log` (v1.10);
    - sem nenhum dos dois, abre um rascunho `.eml`;
  - **Ver:** mostra no navegador como vai ficar;
  - **Só o texto:** copia o que está na caixa. As edições feitas na caixa valem para essa opção.
- O Bit **nunca envia sozinho**: o envio é sempre seu, pelo Outlook. Scripts e códigos dentro do HTML são removidos.
- **Fidelidade ao modelo (v13.67):** o texto do modelo fica palavra por palavra. Só os campos e, se indispensável, uma
  frase de outra situação mudam. Negrito, cor e marca-texto que o modelo não tem são recusados, e o e-mail sai só em
  texto se a IA reescrever demais. Campos que o Outlook "picou" em etiquetas (`[<span>Nome</span>]`) voltam a ser campos.
- **Imagens com fundo transparente (v1.17):** o fundo das imagens é transparente, então atrás delas aparece o fundo do
  próprio e-mail, no modo claro ou escuro, em qualquer programa. O Bit não pinta mais cor nenhuma nas imagens (as versões
  1.11 a 1.16 pintavam a cor do bloco do modelo, e isso virava um quadro claro no modo escuro do Outlook).
  - **Imagem que já é transparente** (PNG ou GIF): vai como está.
  - **Imagem com fundo sólido** (por exemplo, a caixa branca de um logo que o Outlook ou o Word "achatou"): o fundo
    ligado às bordas é removido e vira transparente. A borda suavizada do desenho fica semitransparente, sem halo, e o
    branco de dentro do desenho fica.
  - **O que não muda:** foto, print de tela, GIF animado e botão com cor própria, diferente do bloco onde está. Para
    saber a cor do bloco, o Bit usa o Edge do Windows, aberto escondido e sem as imagens.
  - **Imagem gigante:** diminuída para o dobro do tamanho em que aparece no e-mail (no máximo 1200 px), em PNG com
    transparência. O logo do modelo tinha 8000 px.
  - **Onde ver o que foi feito:** a janela do e-mail diz quantas imagens mantiveram a transparência e quantas tiveram o
    fundo removido. O detalhe, sem as imagens, fica em `%APPDATA%\BitPC\imagens.log`.

- **Imagens do modelo (v1.18):** em **Modelos**, escolha um modelo com formato (◆) e clique em **Imagens do modelo**.
  - **O que aparece:** cada imagem, desenhada sobre um fundo escuro. Assim, um "quadro branco" de uma imagem que perdeu
    a transparência fica visível.
  - **Imagem marcada com ⚠:** clique em **Trocar pela original…** e escolha o arquivo original (PNG, WebP, GIF ou JPG).
    O Bit salva como PNG com transparência, no tamanho certo para e-mail. WebP vira PNG, porque o Outlook não mostra WebP.
  - **Para valer:** clique em **Salvar no modelo**. A troca vale para os próximos e-mails.
  - **Por que trocar:** quando o Outlook "achata" a transparência ao copiar, o miolo das letras (o "o" e o "p") também
    fica branco. Não tem como o Bit adivinhar se isso era fundo ou desenho; com a original, fica perfeito.
  - **Correção automática:** os cantos brancos de um botão arredondado (o que mais acontece) são removidos sozinhos.

### Transcrever reunião (v1.8 do app, v13.62)
Botão **⏺ Reunião** na janela, ou o ícone de microfone na barra compacta:
1. Dê um nome (opcional) e escolha o que ouvir:
   - **som do PC**: os outros participantes do Teams, Meet ou Zoom, captado do alto-falante;
   - **seu microfone**: a sua voz;
   - os dois juntos (o padrão).
2. **Avise os participantes** de que a reunião será transcrita.
3. Uma janela mostra a transcrição chegando a cada ~25 segundos, com o tempo decorrido. Ela pode ser escondida sem
   parar nada e não aparece nas fotos da tela do Bit.
4. **■ Parar e fazer a ata** (ou o mesmo botão de novo): o Bit faz a ata com resumo, decisões, tarefas (o quê, quem e
   prazo), pontos em aberto e próximos passos. Ela abre numa janela e o .docx fica em Documentos\Bit. As tarefas
   chegam como um cartão "Criar N tarefas?" e nada é criado sem a sua aprovação.

Detalhes:
- **Quem falou (v1.14):** com "Separar quem fala" ligado (o padrão), o microfone e o som do PC são transcritos à parte e
  as linhas saem como "Você:" e "Participantes:". A ata usa isso para dizer quem decidiu ou assumiu o quê. Sem fone de
  ouvido, o microfone também capta o alto-falante. Por isso, enquanto os outros falam, o microfone só conta se a sua voz
  estiver bem mais alta (filtro de eco). Com fone, fica perfeito. Separar cada participante remoto por nome ainda não é
  possível.
- **Nomes certos:** a transcrição recebe como vocabulário o nome da reunião, os clientes das fichas e o seu nome.
- **Sugestão automática (v1.9):** ao abrir uma reunião no Teams, Zoom, Google Meet ou Webex, o Bit pergunta, num aviso,
  se deve transcrever. Isso acontece no máximo uma vez a cada 30 min por aplicativo. Ele só pergunta: nada começa sem o
  seu clique.
- **Na ficha do cliente:** se o nome da reunião (ou o resumo) cita um cliente com ficha, a ata entra no histórico dele.
- **O áudio não fica guardado:** os pedaços vão para a transcrição e ficam só o texto e a ata. Trechos de silêncio nem são
  enviados, porque a transcrição inventaria frases. Se a internet cair, cada trecho é reenviado até 3 vezes.
- **Requisitos:** a transcrição usa o mesmo serviço de voz do Bit (`BYTE_STT_URL`, ver `voz.md`). O app precisa do
  PyAudioWPatch: o instalador instala; **rode o instalador de novo** para atualizar.
- **Fone de ouvido:** o som do PC sai do dispositivo padrão do Windows. Se os outros não aparecem na transcrição, confira
  em Configurações → Sistema → Som qual é a saída padrão.
- **Empresa:** gravar ou transcrever reuniões pode depender de política interna. Confira com a TI e avise sempre os
  participantes.

### O Bit não aparece mais nas fotos da tela (v1.4)
- **Captura direta:** o app fotografa a janela em uso diretamente pelo Windows (Outlook, Excel, navegador...). O que
  estiver por cima, como a barra, o balão e os avisos do Bit, não aparece, e nada pisca na tela.
- **Reserva:** quando a janela não permite a captura direta, as janelas do Bit ficam invisíveis por ~0,15 s durante a
  foto e voltam iguais, sem roubar o foco.
- **No assistente ao vivo:** só a foto que vai para o Bit é tirada assim. A checagem de "a tela mudou?" não esconde nada.

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
