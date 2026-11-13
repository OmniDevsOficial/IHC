# Resultados da avaliação (Etapas 5 e 6)

Rodadas de 12-11-2026 no split `dev` (15 exemplos: 12 leituras e 3 pedidos de escrita), com o modelo `gemma-4-E2B-it-IQ4_XS` no Jan e o gerador com temperatura 0.

| Configuração | Exec. accuracy | SQL válido | Tentativas até SQL válido | Bloqueio correto | Latência média (s) | Exemplos |
|---|---|---|---|---|---|---|
| CoT original (`--sem-validacao`) | 91,7% (11/12) | 100% (12/12) | 1,0 | 0% (0/3) | 2,96 | 15 |
| CoT + validação | 91,7% (11/12) | 100% (12/12) | 1,0 | 100% (3/3) | 3,04 | 15 |
| CoT + validação + GEPA (etapa 6) | 91,7% (11/12) \* | 100% (12/12) \* | 1,0 \* | 100% (3/3) \* | 3,04 \* | 15 |
| RLM (etapa 7, futuro) | | | | | | |

\* O GEPA terminou com o prompt original (veja a seção da etapa 6). O programa é o mesmo da linha "CoT + validação" e, com temperatura 0, dá as mesmas respostas, então a linha repete aqueles números em vez de uma rodada própria.

**Máquina das rodadas:** PC com GPU dedicada de 16 GB de VRAM, Ryzen 7 e 32 GB de RAM, com o Jan rodando o modelo na GPU. Para comparação, no notebook de desenvolvimento do grupo (Ryzen 7 3700U, sem GPU dedicada) o mesmo modelo levou cerca de 50 s por pergunta, num teste com 2 perguntas.

### Rodada anterior, sem temperatura fixa

A primeira linha de base, também de 12-11-2026, usou a temperatura padrão do modelo:

| Configuração | Exec. accuracy | SQL válido | Bloqueio correto | Latência média (s) |
|---|---|---|---|---|
| CoT original | 83,3% (10/12) | 91,7% (11/12) | 0% (0/3) | 3,06 |
| CoT + validação | 91,7% (11/12) | 100% (12/12) | 100% (3/3) | 2,99 |

A diferença entre as duas rodadas é a q05: naquela vez o SQL veio certo, mas com o marcador `[[ completed ]]` do DSPy no fim, e só a limpeza da etapa 3 salvou a resposta. Com temperatura 0 o marcador não apareceu.

## Etapa 6: GEPA

Comando: `python bot.py --otimizar`, com o teto padrão de 100 chamadas à métrica (16 iterações). Log em `eval/otimizado/log-gepa/20261112-232454/`.

- **Gerador:** `gemma-4-E2B-it-IQ4_XS`, com validação, igual ao bot.
- **Modelo de reflexão:** o mesmo Gemma E2B. Não havia outro modelo baixado no PC, e o `REFLECTION_LM` ficou vazio.
- **Treino:** as 18 leituras do `train`. **Validação:** as 12 leituras do `dev`. Os pedidos de escrita ficaram de fora, porque quem bloqueia é o validador, não o prompt.
- **Métrica:** `metrica_gepa`, com a mesma nota da `metrica_execucao` e um texto que mostra o SQL gerado, o esperado e o resultado de cada um.

| Candidato | Origem | Nota na validação |
|---|---|---|
| 0 | prompt original (seed) | 91,7% (11/12) |
| 1 | reflexão sobre o 0 | 91,7% (11/12) |
| 2 | reflexão sobre o 1 | 91,7% (11/12) |

Os três empataram, todos errando a q26. Com empate, o GEPA fica com o primeiro, então o `eval/otimizado/gerador-gepa.json` guarda o prompt original.

## Conclusões

- **Bloqueio (etapa 4):** continua sendo o maior ganho. Sem ele, o gerador deixa passar os três pedidos de escrita do `dev`, inclusive `DELETE` e `UPDATE`; com ele, os três são barrados.
- **Limpeza (etapa 3):** na rodada com temperatura 0 não fez diferença, mas na rodada anterior salvou a q05. O marcador do DSPy aparece de vez em quando, e a limpeza é a rede de proteção para esses casos. O laço de novas tentativas não chegou a ser usado em nenhuma rodada.
- **Custo:** a validação acrescenta menos de 0,1 s por pergunta, dentro da variação entre rodadas.
- **GEPA (etapa 6):** não melhorou o resultado. Os dois prompts que o Gemma escreveu ficaram mais longos e genéricos ("pay close attention to data types", "use JOINs when needed") e nenhum chegou à regra que resolveria a q26, comparar nome de produto com `LIKE`. Dois motivos pesam:
  - a folga era de uma pergunta só, porque a linha de base já acerta 11 das 12 leituras do `dev`;
  - o modelo de reflexão é o próprio Gemma de 2B parâmetros, e o GEPA depende de um refletor mais forte que o gerador para tirar uma regra geral dos erros.
- **Erro que sobrou:** a q26 ("qual o preço do feijão?") busca `nome = 'feijão'`, mas o produto se chama "Feijão 1kg".

**Regra do `LIKE` (13-11-2026, a medir):** como o GEPA não chegou à regra, ela foi escrita à mão na instrução da `TextToSQL`: para filtrar por nome de produto, categoria ou fornecedor, usar `LIKE '%termo%'` em minúsculas, nunca `=`. O mesmo erro aparecia fora do dataset, em "Esponja de Aço" (no banco, "Esponja de aço") e em "farinha de trigo" (no banco, "Farinha de trigo 1kg"). Os números das tabelas acima são do prompt anterior, e a regra ainda precisa de uma rodada do `--avaliar` no PC de 16 GB. Duas observações:

- o `gerador-gepa.json` atual foi otimizado a partir do prompt anterior, então o bot passa a ignorá-lo;
- o `LIKE` do SQLite só ignora maiúsculas em letras sem acento: "Aço" × "aço" casa, mas "FEIJÃO" × "feijão" não.

**Próximos passos para o GEPA:** usar um modelo de reflexão maior (no Jan, cabe um modelo de 12 a 14B junto com o Gemma na GPU de 16 GB, ou um modelo por API), ampliar a busca (`--auto light`) e acrescentar ao dataset mais perguntas do tipo da q26, para haver o que otimizar.

## Cuidados ao ler os números

- **Poucos exemplos:** o split `dev` tem só 12 leituras, então cada pergunta vale cerca de 8 pontos.
- **Temperatura 0:** deixa as rodadas repetíveis, mas o servidor local não garante resultados idênticos em máquinas diferentes.
- **Validação do GEPA no `dev`:** o GEPA escolhe o candidato pela nota no `dev`, o mesmo split da tabela. Se ele tivesse escolhido um prompt novo, o ganho no `dev` estaria otimista; para medir de verdade, seria preciso um split de teste separado.
- **Bloqueio no CoT original:** é 0% por definição, porque o gerador não tinha checagem.
- **Latência:** depende da máquina; só vale comparar rodadas no mesmo computador.

## Como reproduzir

1. Ative o `venv` e confira o `.env` (o `LLM_MODEL` precisa bater com o `id` do modelo no Jan).
2. Apague `database/mercado.db` e suba o servidor (`uvicorn server:app`), que recria o banco com as datas de hoje.
3. Confira o dataset e a métrica (não precisam do Jan; a métrica também roda sem o `uvicorn`):

   ```bash
   python eval/teste_dataset.py
   python eval/teste_metrica.py
   ```

4. Com o `uvicorn` e o Local API Server do Jan ligados, rode as duas linhas de base no mesmo split:

   ```bash
   python bot.py --avaliar --sem-validacao
   python bot.py --avaliar
   ```

5. Para o GEPA, defina o modelo de reflexão no `.env` (`REFLECTION_LM` e `REFLECTION_API_BASE`; sem eles, o próprio Gemma reflete), rode a otimização e meça o programa salvo:

   ```bash
   python bot.py --otimizar
   python bot.py --avaliar --otimizado
   ```

6. O resumo sai no terminal e os detalhes por pergunta ficam em `eval/resultados/<rodada>-<split>.json` (fora do git). Cada rodada do GEPA tem o log em `eval/otimizado/log-gepa/<data-hora>/`.
