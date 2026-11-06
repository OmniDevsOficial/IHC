# Resultados da Avaliação (Etapa 5)

Rodada ainda não executada.

| Configuração | Exec. accuracy | SQL válido | Tentativas (média) | Bloqueio correto | Latência (s) | Exemplos |
|---|---|---|---|---|---|---|
| CoT original (`--sem-validacao`) | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada |
| CoT + validação | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada |
| GEPA (etapa 6) | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada |
| RLM (etapa 7, futuro) | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada | Rodada ainda não executada |

## Como reproduzir

1. Ative o ambiente virtual do projeto (`.\venv\Scripts\Activate.ps1` ou `source ./venv/Scripts/activate`).
2. Confira a integridade do dataset e dos SQLs de referência (não requer o Jan ligado):
   ```bash
   python eval/avaliar.py --seco
   ```
3. Ligue o **Local API Server** no Jan com o modelo configurado no `.env` (não é necessário subir o `uvicorn`, pois o avaliador acessa o banco diretamente em modo somente leitura).
4. Execute a avaliação do CoT original (`--sem-validacao`) e do CoT com validação (`validar=True`):
   ```bash
   python eval/avaliar.py --split dev --sem-validacao --nome cot-original
   python eval/avaliar.py --split dev --nome cot-validacao
   ```
5. Caso uma rodada seja interrompida, continue do ponto em que parou adicionando `--retomar`:
   ```bash
   python eval/avaliar.py --split dev --nome cot-validacao --retomar
   ```
6. Os resultados agregados ficam salvos em `eval/resultados/<nome>.resumo.json` e o detalhamento pergunta a pergunta em `eval/resultados/<nome>.jsonl`.

## Limitações

- **Latência do modelo local**: em máquinas rodando em CPU (ou GPUs de entrada), cada chamada ao LLM local pode levar vários minutos — especialmente nas perguntas que exigem até 3 tentativas na configuração com validação.
- **Rodadas parciais (`--limite`)**: caso o tempo total inviabilize rodar todo o conjunto de uma vez, pode-se usar `--limite N`. Para uma comparação justa na tabela acima, as rodadas de **CoT original (`--sem-validacao`)** e **CoT + validação** devem obrigatoriamente usar o **MESMO `--split`** e o **MESMO `--limite`**.
