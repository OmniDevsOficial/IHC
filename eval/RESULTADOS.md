# Resultados da avaliação (Etapa 5)

Linha de base medida em 12-11-2026 no split `dev` (15 exemplos: 12 leituras e 3 pedidos de escrita), com o modelo `gemma-4-E2B-it-IQ4_XS` no Jan.

| Configuração | Exec. accuracy | SQL válido | Tentativas até SQL válido | Bloqueio correto | Latência média (s) | Exemplos |
|---|---|---|---|---|---|---|
| CoT original (`--sem-validacao`) | 83,3% (10/12) | 91,7% (11/12) | 1,0 | 0% (0/3) | 3,06 | 15 |
| CoT + validação | 91,7% (11/12) | 100% (12/12) | 1,0 | 100% (3/3) | 2,99 | 15 |
| GEPA (etapa 6) | | | | | | |
| RLM (etapa 7, futuro) | | | | | | |

**Máquina das rodadas:** PC com GPU dedicada de 16 GB de VRAM, Ryzen 7 e 32 GB de RAM, com o Jan rodando o modelo na GPU. Para comparação, no notebook de desenvolvimento do grupo (Ryzen 7 3700U, sem GPU dedicada) o mesmo modelo levou cerca de 50 s por pergunta, num teste com 2 perguntas.

## Conclusões

- **Limpeza (etapa 3):** ganhou a q05, que veio com o SQL certo mas com o marcador `[[ completed ]]` do DSPy no fim. O laço de novas tentativas não chegou a ser usado.
- **Bloqueio (etapa 4):** é o maior ganho. Sem ele, o gerador deixou passar `DELETE` e `UPDATE`; com ele, os três pedidos foram barrados.
- **Custo:** a validação não acrescenta tempo.
- **Erro que sobrou:** a q26 ("qual o preço do feijão?") busca `nome = 'feijão'` em vez de usar `LIKE`. É o primeiro alvo do GEPA.

## Cuidados ao ler os números

- **Poucos exemplos:** o split `dev` tem só 12 leituras, então cada pergunta vale cerca de 8 pontos, e a diferença entre as configurações é de uma pergunta só.
- **Uma rodada só:** a temperatura do modelo não é fixada, então rodar de novo pode mudar uma ou outra resposta.
- **Bloqueio no CoT original:** é 0% por definição, porque o gerador não tinha checagem.
- **Latência:** depende da máquina; só vale comparar rodadas no mesmo computador.

## Como reproduzir

1. Ative o `venv` e confira o `.env` (o `LLM_MODEL` precisa bater com o `id` do modelo no Jan).
2. Apague `database/mercado.db` e suba o servidor (`uvicorn server:app`), que recria o banco com as datas de hoje.
3. Confira o dataset e a métrica (não precisam do Jan nem do `uvicorn`):

   ```bash
   python eval/teste_dataset.py
   python eval/teste_metrica.py
   ```

4. Com o `uvicorn` e o Local API Server do Jan ligados, rode as duas configurações, uma depois da outra, no mesmo split:

   ```bash
   python bot.py --avaliar --sem-validacao
   python bot.py --avaliar
   ```

5. O resumo sai no terminal e os detalhes por pergunta ficam em `eval/resultados/<rodada>-<split>.json` (fora do git).
