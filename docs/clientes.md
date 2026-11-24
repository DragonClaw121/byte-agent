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

## O que o Bit faz
1. **Identifica o cliente:** nome e CNPJ, pelo pedido, pela tela ou pelas notas do assistente.
2. **Puxa o cadastro oficial** pelo CNPJ (Receita Federal, via BrasilAPI): razão social, nome fantasia, situação,
   abertura, atividade principal e secundárias, porte, capital social, endereço e **sócios e administradores**. Sem
   CNPJ, ele procura "<nome> CNPJ" e só aceita o número se o nome do cadastro bater.
3. **Faz buscas direcionadas** com o nome exato: site, notícias, pessoas-chave, Reclame Aqui, expansão e contratos.
   **Só ficam as fontes que mencionam o cliente.** Antes, vinham pesquisas de satisfação e criptomoedas.
4. **Junta os dados internos** (compras, pedidos, valores, datas).
5. **Monta o plano de conta** em Word (.docx), com:
   - cadastro oficial e sócios;
   - o cliente em resumo e o momento atual;
   - pessoas-chave e decisores;
   - relação com a gente (compras);
   - oportunidades;
   - riscos;
   - estratégia de acionamento: canal, quem procurar, melhor momento, argumento, oferta e objeções;
   - próximos passos;
   - o **e-mail de abordagem** pronto;
   - as fontes.

Cada informação diz de onde veio: [n] para a web, [CNPJ] para o cadastro e [interno] para os seus dados. O que não foi
encontrado aparece como "não encontrado": nada é inventado.

No Bit do PC, o e-mail de abordagem abre numa janela (copiar ou abrir no Outlook) e o .docx fica em Documentos\Bit.

## Limites
- **Cadastro:** só para empresas brasileiras (CNPJ). Para empresas de fora, sai só a parte da web.
- **Busca na web:** a qualidade depende do buscador configurado. Com o Tavily (`TAVILY_API_KEY`) é bem melhor.
- **Dados internos:** o Bit não acessa o seu ERP ou CRM diretamente. Ele usa o que aparece na tela ou o que você
  informar. Desde a v13.59, quando a conversa fala em consultar o ERP/sistema, ele diz isso com clareza e explica como
  mostrar os dados (Olhar a tela ou Ao vivo), em vez de pedir "autorização" para uma consulta que não existe.
