# Planilhas e documentos (v13.26), com o Bit

Peça em linguagem natural, no Telegram do Bit ou no painel. O arquivo chega pronto no Telegram e fica na pasta
`entregas/`.

## Planilhas (.xlsx)
> "Monte uma planilha de orçamento de casa: aluguel 1500, mercado 900, luz 180, internet 120, com gráfico de pizza."

O Bit cria:
- colunas com **tipo**: texto, número, moeda (R$), porcentagem e data, no formato brasileiro;
- **fórmulas de verdade**, que recalculam quando você muda um valor: subtotal por linha, `SE`, `PROCV` etc.;
- a **linha de Total** com `SOMA`;
- filtros, cabeçalho fixo e **gráfico** de barras, linhas ou pizza.

O arquivo abre no Excel, no Google Planilhas e no LibreOffice.

As fórmulas podem vir em português (`SOMA`, `SE`, `PROCV`, `MÉDIA`) e são gravadas no formato do Excel. Por
segurança, só funções de cálculo são aceitas: links (`HIPERLINK`, `WEBSERVICE`), referências a outros arquivos e
comandos externos são recusados.

Para **analisar** uma planilha que você já tem, envie o arquivo e peça a análise. Isso continua sendo a ferramenta de
análise, que calcula os números por código.

## Documentos (.docx)
> "Escreva uma proposta de consultoria para a Padaria Sol: escopo, cronograma em tabela e investimento."

O Bit cria um documento Word com título, seções, parágrafos, listas e tabelas. Ele não inventa dados: onde faltar uma
informação, escreve `[PREENCHER]`.

## Seus modelos (o seu visual)
1. No Word ou no Excel, faça o modelo com o seu logo, cabeçalho, rodapé e fontes. Onde o Bit deve preencher, escreva
   marcadores entre chaves duplas: `{{cliente}}`, `{{valor}}`, `{{data}}`.
2. Num .docx, escreva `{{conteudo}}` sozinho numa linha, no lugar onde o texto gerado deve entrar (seções, listas,
   tabelas).
3. Envie o arquivo para o Bit no Telegram, como documento. Dica: comece o nome com `modelo`, por exemplo
   `modelo_proposta.docx`.
4. Peça: "faça uma proposta para a Padaria Sol, R$ 4.000, usando o modelo_proposta".

`/modelos` lista os seus modelos e os campos de cada um. Um campo sem valor vira `[PREENCHER: campo]` e o Bit avisa
qual faltou. A formatação de cada trecho (negrito, cor) é mantida.

## Requisitos
Os pacotes `openpyxl` e `python-docx` vêm no extra `trabalho`, que a nuvem já instala nas atualizações. No computador:
```
pip install -e ".[trabalho]"
```
Se você tem um `bit.json` próprio, inclua `"documentos"` em `habilidades`. O manifesto que vem com o projeto já tem.
