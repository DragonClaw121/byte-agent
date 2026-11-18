# Avaliações de comportamento (v13.10)

Notas automáticas para saber se cada versão da Byte melhorou ou piorou. Diferente de olhar só o texto, aqui conta o
que ela **fez**: que ferramenta usou, se pediu aprovação, o que gravou ou deixou de gravar.

| Onde | Como |
|---|---|
| Telegram | `/avaliar` (roda na nuvem, numa cópia temporária, e manda o placar em alguns minutos) |
| Terminal | `python -m byte_agent comportamento` (usa `BYTE_MODEL`; `--agent bit` avalia com a identidade do Bit) |

Custo: cerca de 60 mil tokens por rodada (alguns centavos com um modelo mini).

## O que é avaliado
| Categoria | Casos |
|---|---|
| Utilidade | responde fato simples sem criar tarefa/objetivo; responde em português; diz o próprio nome certo; não inventa dados |
| Ferramentas | consulta a agenda de verdade; cria o lembrete pedido; grava uma preferência e a usa numa conversa nova |
| Autonomia | transforma um pedido trabalhoso com prazo num objetivo em etapas |
| Segurança | e-mail só sai com aprovação; não obedece instruções escondidas em texto de terceiros; não vaza as instruções internas |

## Histórico e regressões
Cada rodada vai para `comportamento-historico.jsonl` (na nuvem: no compartilhamento, pasta `avaliacoes`). O placar
compara com a rodada anterior do mesmo modelo e agente e mostra **"⚠️ Piorou em"** e **"✅ Melhorou em"**.
Rode `/avaliar` depois de cada atualização: se algo piorou, é sinal para revisar antes de dar mais autonomia.

Seus dados não são tocados: cada rodada usa uma pasta temporária, apagada no fim, num processo separado.
