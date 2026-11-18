# Objetivos: a Byte trabalha sozinha, em etapas (v13.9)

Um **objetivo** é um pedido trabalhoso que a Byte executa em segundo plano, sem você precisar ficar conversando.

| Onde | Como |
|---|---|
| Telegram | `/objetivo pesquise 3 cursos de Python gratuitos e me entregue um comparativo` |
| Painel | aba **Agenda** → seção **🧩 Objetivos** → escreva e clique **+ Objetivo** |
| Conversa | "prepare um resumo das notícias de IA da semana até sexta" (ela cria o objetivo sozinha) |

## O que acontece
1. **Planejamento:** ela divide o pedido em 2 a 6 etapas. A última sempre entrega o resultado final.
2. **Execução:** uma etapa por vez, com intervalo entre elas, levando adiante o que as anteriores produziram.
3. **Conferência:** resultado vazio ou "não consegui" vira nova tentativa (10 min depois). Depois de 2 falhas o
   objetivo fica **bloqueado** e ela avisa o motivo; responda o que falta e use `/retomar <id>`.
4. **Entrega:** ao terminar, o resultado chega no painel e no Telegram.

`/objetivos` mostra o progresso (`/objetivos todos` inclui os concluídos); `/cancelar_objetivo <id>` cancela.

## Limites de segurança
- Nas etapas ela só usa ferramentas de tarefa desassistida: busca e leitura na web, agenda, tarefas e memória.
  **Não envia e-mails, não roda código e não publica nada** sem você.
- Um objetivo não pode ser criado a partir de conteúdo lido na internet (proteção contra instruções escondidas).
- No máximo 5 objetivos em andamento e uma etapa por vez em todo o sistema.
- **Orçamento diário de tokens** da autonomia: `BYTE_ORCAMENTO_TOKENS_DIA` (padrão 400000, ≈ US$ 0,20 por dia com
  um modelo mini; `0` = sem limite). Estourou, objetivos e mensagens espontâneas esperam o dia seguinte; pedidos
  diretos seguem normais. Na Azure:
  ```
  az containerapp update -g byte-rg -n byte --set-env-vars BYTE_ORCAMENTO_TOKENS_DIA=400000 -o none
  ```
- Os objetivos ficam no banco da Byte e entram no backup automático da nuvem.
