# Pesquisa de cliente com plano de abordagem (v13.50)

Para pesquisar um cliente (ou um prospect, fornecedor ou qualquer empresa brasileira) e sair com um **plano de como
acioná-lo**, e não com um resumo genérico da web.

## Como pedir
- **No Bit do PC**, com a ficha do cliente aberta na tela (ERP, CRM, planilha de pedidos), escreva "pesquise esse
  cliente" ou clique em **⌕ Cliente**. O Bit lê na tela quem é o cliente e o histórico de compras. Com o **assistente
  ao vivo** ligado, ele usa tudo o que anotou enquanto você navegava pelas telas.
- **No painel (aba ⚡ Comandos), no app ou no Telegram:**
  ```
  /cliente ASBEC Sociedade Baiana de Educação | comprou 3 lotes de notebooks em 2025 (R$ 180 mil), último pedido em junho
  ```
  Antes da barra vai o nome ou o CNPJ. Depois dela, o que você sabe das compras (opcional, mas é o que torna a
  estratégia boa).
- **Na conversa:** "faça um levantamento do cliente ACME Ltda" também funciona, assim como "continue a análise",
  "refaz aquele resumo profundo sobre o cliente" ou "a abordagem desse cliente" (v13.59).
- **Juntando várias telas (v13.59):** cada **Olhar a tela** guarda o que o Bit leu (clientes, CNPJs, pedidos,
  produtos, valores, datas) por 12 horas. Abra no ERP a lista de vendas do cliente, depois a de outro CNPJ do mesmo
  grupo, olhando cada uma, e peça a análise: ele junta tudo, mais o que você escreveu na conversa. "Esse cliente" sem
  nome é o mais recente que apareceu.

- **Com recortes (v13.75):** aperte **Ctrl+Alt+R** e recorte as partes que importam. Por exemplo, as compras no ERP,
  o cadastro no CRM e a página de pessoas do LinkedIn da empresa (troque de página e aperte de novo). Depois escreva
  "pesquise esse cliente": tudo vai numa mensagem só.

## O que o Bit faz
1. **Identifica o cliente:** nome e CNPJ, pelo pedido, pela tela ou pelas notas do assistente.
2. **Puxa o cadastro oficial** pelo CNPJ (Receita Federal, via BrasilAPI): razão social, nome fantasia, situação,
   abertura, atividade principal e secundárias, porte, capital social, endereço e **sócios e administradores**. Sem
   CNPJ, ele procura "<nome> CNPJ" e só aceita o número se o nome do cadastro bater.
3. **Faz buscas direcionadas** com o nome exato: site, notícias, pessoas-chave, Reclame Aqui, expansão e contratos.
   **Só ficam as fontes que mencionam o cliente.** Antes, vinham pesquisas de satisfação e criptomoedas.
4. **LinkedIn (v13.75):** procura a página da empresa e os perfis públicos de pessoas de lá (diretores, gerentes,
   coordenadores, compras, TI, financeiro).
   - **Como procura:** pelo buscador (Tavily, Brave ou SearXNG), com o filtro só do LinkedIn, e usa apenas o título e o
     trecho que o buscador mostra. Só ficam resultados que mencionam o cliente.
   - **O que ele não faz:** não abre as páginas do LinkedIn, porque elas pedem login e o LinkedIn proíbe coleta
     automática. Também não usa a sua conta.
   - **Para ir além:** abra o LinkedIn você mesmo e mande recortes da tela (Ctrl+Alt+R). As pessoas e os cargos que
     aparecem nos recortes também entram no plano, marcados como [tela].
   - **O que entra no plano:**
     - uma seção **"LinkedIn: empresa e pessoas"**: a página da empresa e uma tabela com pessoa, cargo, por que procurá-la
       e o link do perfil;
     - um **convite de conexão** sugerido (até 280 caracteres);
     - o que olhar no LinkedIn com essa conta (vagas, posts, quem entrou).
   - **Cuidados:**
     - os links vêm sempre da busca, e o Bit apaga qualquer link que a IA tenha inventado;
     - o cargo pode estar desatualizado: confirme antes de usar;
     - as pessoas também entram nos contatos da ficha do cliente.
   - **Requisito:** um buscador da web configurado (`TAVILY_API_KEY`, `BRAVE_API_KEY` ou `BYTE_SEARXNG_URL`). Só com a
     Wikipédia, não há LinkedIn.
5. **Junta os dados internos** (compras, pedidos, valores, datas).
6. **Monta o plano de conta** em Word (.docx), com:
   - cadastro oficial e sócios;
   - o cliente em resumo e o momento atual;
   - pessoas-chave e decisores, e o LinkedIn (empresa, pessoas e convite de conexão);
   - relação com a gente (compras);
   - oportunidades;
   - riscos;
   - estratégia de acionamento: canal, quem procurar, melhor momento, argumento, oferta e objeções;
   - próximos passos;
   - o **e-mail de abordagem** pronto;
   - as fontes.

Cada informação diz de onde veio: [n] para a web, [Ln] para o LinkedIn, [CNPJ] para o cadastro, [tela] para o que
estava nos recortes e [interno] para os seus dados. O que não foi encontrado aparece como "não encontrado": nada é inventado.

No Bit do PC, o e-mail de abordagem abre numa janela (copiar ou abrir no Outlook) e o .docx fica em Documentos\Bit.

## Ficha do cliente (v13.60)
Cada cliente pesquisado ganha uma **ficha que fica guardada** e cresce com o tempo:
- **O que guarda:**
  - nomes (razão social, fantasia, como você chama);
  - CNPJs, inclusive de outras unidades do grupo;
  - pessoas-chave;
  - compras e dados internos, acumulados sem repetir;
  - resumo, próximos passos e o último plano de conta;
  - o **histórico**: pesquisas, e-mails preparados, telas lidas e notas suas.
- **Quem escreve:** a pesquisa de cliente, o Bit no PC (cada tela lida que cita o cliente e cada e-mail preparado para
  ele), as **atas de reunião** com o cliente (pelo nome da reunião ou pelo resumo; os próximos passos e as tarefas viram
  os próximos passos da ficha), os **e-mails recebidos** dele na triagem (v13.63) e você.
- **No resumo da manhã (v13.64):** a seção "🗂️ Clientes — próximos passos" lista os clientes com ficha mexida nas
  últimas 3 semanas que têm próximos passos.
- **Na triagem**, o e-mail de um cliente com ficha aparece com "🗂️ cliente X", e o rascunho da resposta usa a ficha.
- **Quem usa:** a próxima pesquisa (o plano novo parte do que já sabíamos e mostra o que mudou), o **Preparar e-mail**
  e o **Olhar a tela** do PC, e a conversa em qualquer canal. Basta citar o cliente ("quando renova o Figma da EMS?")
  e a ficha entra na resposta. Um CNPJ de outra filial, com a mesma raiz, cai na mesma ficha.

| Comando | O que faz |
|---|---|
| `/clientes` | lista as fichas, das mais recentes para as mais antigas |
| `/ficha EMS` (ou o número ou o CNPJ) | mostra tudo o que o Bit sabe do cliente |
| `/nota_cliente EMS | renovação do Figma em março; falar com a Júlia` | anota; se for sobre compra/renovação, entra nas compras |
| `/apagar_ficha EMS` | apaga a ficha e o histórico |
| `/data_cliente EMS | renovação Figma | 10/03/2027` | guarda uma data (renovação, compra, vencimento) |
| `/renovacoes` | as renovações dos próximos 90 dias |
| `/alertas_clientes` | quando avisar (renovação e sem contato) |

### Datas, renovações e alertas (v13.67)
A ficha guarda as **renovações**, **compras** e **vencimentos** de cada cliente e o **último contato**:
- **Como as datas entram:**
  - `/data_cliente EMS | renovação Figma Enterprise | 10/03/2027`;
  - numa nota: `/nota_cliente EMS | renovação do Figma em 10/03/2027` (o Bit reconhece 10/03/2027, 10/03, "10 de
    março de 2027", "março de 2027", 03/2027);
  - na conversa: "a renovação do Miro da EMS é 15/05/2027";
  - pela pesquisa de cliente, com os dados de compra: uma licença **ANUAL** comprada em 28/10/2026 vira uma renovação
    **estimada** em 28/10/2027. Quando você informar a data certa, ela substitui a estimada.
- **O último contato** é atualizado por e-mails recebidos dele (triagem), e-mails preparados, reuniões e notas como
  "liguei", "falei com", "enviei".
- **Os alertas** chegam no Telegram, no painel, no app e no Bit do PC:
  - renovação aos **60, 30, 15, 7 e 1 dia(s)** antes, uma vez por marco, com a sugestão de preparar o e-mail de renovação;
  - **toda segunda**, a lista dos clientes com compras e **sem contato há 45+ dias**.
  - Para mudar: `/alertas_clientes 60,30,15,7 | 45` (dias antes da renovação | dias sem contato).
- **`/renovacoes`** lista as renovações e vencimentos dos próximos 90 dias (`/renovacoes 365` para o ano). O **resumo da
  manhã** mostra as dos próximos 30 dias, e `/clientes` mostra a próxima renovação de cada um (🔁).

Também no painel e no app, em **⚡ Comandos → Clientes**. A ficha fica no banco do próprio agente: cada convidado
tem as suas.

## Limites
- **Cadastro:** só para empresas brasileiras (CNPJ). Para empresas de fora, sai só a parte da web.
- **Busca na web:** a qualidade depende do buscador configurado. Com o Tavily (`TAVILY_API_KEY`) é bem melhor.
- **Dados internos:** o Bit não acessa o seu ERP ou CRM diretamente. Ele usa o que aparece na tela ou o que você
  informar. Desde a v13.59, quando a conversa fala em consultar o ERP/sistema, ele diz isso com clareza e explica como
  mostrar os dados (Olhar a tela ou Ao vivo), em vez de pedir "autorização" para uma consulta que não existe.
