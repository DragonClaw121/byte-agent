# Byte na internet

A Byte busca na web, lê páginas e faz pesquisas profundas com relatório e fontes citadas. É **só leitura**: ela não
preenche formulários, não faz login em sites e não baixa arquivos binários.

## Usar

| Onde | Como |
|---|---|
| Conversa (modelos com ferramentas, 7B+) | "pesquise as últimas notícias sobre X", "resuma https://site/artigo", "faça uma pesquisa profunda sobre Y" |
| Telegram | `/pesquisar <pergunta>` (relatório chega como arquivo) · `/buscar <termos>` (lista de links) |
| Painel | aba Trabalho → "Pesquisa na web" |
| Rotinas | `/rotina 08:00 pesquise notícias de energia solar e me resuma` |

Modelos sem ferramentas nativas não navegam sozinhos durante a conversa; para eles, use `/pesquisar` ou o painel.

## Buscadores

A **Wikipédia** funciona sempre, sem chave. Para busca na web em geral, configure um destes (Colab Secrets ou `.env`):

| Variável | Serviço | Custo |
|---|---|---|
| `BYTE_SEARXNG_URL` | SearXNG auto-hospedado (ative `formats: [html, json]` no `settings.yml`) | gratuito |
| `TAVILY_API_KEY` | Tavily (feito para agentes de IA) | plano gratuito com cota mensal |
| `BRAVE_API_KEY` | Brave Search API | pago por uso |

O primeiro configurado é usado; se falhar, a Byte tenta o próximo e, por último, a Wikipédia.
`BYTE_WEB=0` desliga o acesso à internet.

## Proteções

- **Rede interna bloqueada.** A Byte não abre endereços internos: o próprio Ollama, o painel, a rede da sua casa ou
  da empresa e o servidor de metadados do Colab (que guarda credenciais). O endereço é verificado depois da
  resolução de DNS, a conexão é feita no IP verificado, e cada redirecionamento é verificado de novo.
- **Instruções escondidas em páginas.** Depois de ler a web, a Byte não grava tarefas, memórias nem lembretes no
  mesmo pedido; ela diz o que gravaria e pede confirmação numa nova mensagem. Código e e-mail continuam exigindo
  aprovação, como sempre.
- **Vazamento de dados por link.** A Byte só abre links que vieram da sua mensagem, de uma busca ou de uma página já
  lida, exatamente como apareceram. Ela não consegue montar um link novo com dados seus dentro.
- **Fontes conferidas.** Na pesquisa profunda, toda afirmação deve citar a fonte `[n]`; citações para fontes que não
  existem aparecem marcadas no relatório. Mesmo assim, o texto é redigido por IA: confira as fontes antes de decidir.

## Limitações desta versão

- Sem JavaScript: páginas que só mostram conteúdo após carregar scripts aparecem vazias.
- Sem PDF: apenas HTML, texto e JSON.
- Redes com proxy obrigatório (comum em empresas) não são suportadas ainda.
- Pesquisas sobre pessoas físicas envolvem a LGPD: prefira fontes públicas e não guarde dados pessoais sem necessidade.
