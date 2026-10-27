# Text-to-SQL via Telegram — IHC

Projeto da disciplina de **Interação Humano-Computador (IHC)** — 3º DSM / Fatec.

Explora uma forma natural de interação com dados: em vez de escrever SQL, o usuário pergunta em português a um bot do Telegram — por texto ou áudio — e o sistema traduz a pergunta em consulta SQL, executa no banco e devolve o resultado em texto.

```
usuário (texto ou voz) → Telegram → Whisper → DSPy + LLM local → SQL → API FastAPI → SQLite → resposta
```

## Estrutura

```
bot.py               # bot do Telegram (Whisper + DSPy); consulta o banco pela API
server.py            # API FastAPI + banco SQLite (cria e popula o banco)
.env.example         # modelo das variáveis de ambiente (copie para .env)
requirements.in      # dependências diretas (é este que você edita)
requirements.txt     # lock gerado pelo pip-compile (não edite à mão)
database/            # criado automaticamente pelo servidor (não vai para o git)
```

## Requisitos

- **Python 3.12** (o lock foi gerado nele; versões mais novas podem não instalar o torch e o whisper)
- [Jan.ai](https://jan.ai/) com um modelo carregado
- ffmpeg (para o áudio)
- Um bot do Telegram (token do [@BotFather](https://t.me/BotFather))

## Setup

```bash
python -m venv venv                   # cria o ambiente virtual (Windows: py -3.12 -m venv venv)
source ./venv/Scripts/activate        # ativa no Git Bash (Linux: source venv/bin/activate)
pip install pip-tools                 # instala o gerenciador de dependências
pip-sync requirements.txt             # espelha o ambiente com o lock
cp .env.example .env                  # copia o template e preenche o token (ver notas)
```

No PowerShell, ative o ambiente com `.\venv\Scripts\Activate.ps1`.
Se o `pip-sync` não for encontrado, use `python -m piptools sync requirements.txt`.

## Rodar

Siga esta ordem, cada item em um terminal (o bot busca o schema no servidor, então o servidor sobe primeiro):

1. Abra o Jan, carregue o modelo e ligue o **Local API Server**.
2. Servidor:
```bash
   uvicorn server:app --reload
```
3. Bot (outro terminal, com o ambiente virtual ativo):
```bash
   python bot.py
```

## Rota da API

Uma única rota, `GET /query`:

| Uso | Exemplo |
|---|---|
| Executar uma consulta SELECT | `GET /query?sql=SELECT nome FROM categorias` |
| Obter o schema (descrição + DDL das tabelas) | `GET /query?schema=true` |

Com `schema=true`, o parâmetro `sql` é ignorado. É por essa rota que o bot busca o schema (uma vez, com cache). Sem `sql` e sem `schema`, a resposta é erro 400. Só consultas `SELECT` são aceitas.

## Variáveis do `.env`

| Variável | Descrição | Padrão |
|---|---|---|
| `TELEGRAM_API_TOKEN` | Token do bot (obrigatório) | — |
| `SERVER_URL` | Endereço do servidor FastAPI | `http://127.0.0.1:8000` |
| `LLM_MODEL` | Nome do modelo no Jan, com prefixo `openai/` | `openai/gemma-4-E2B-it-IQ4_XS` |
| `LLM_API_BASE` | Endereço da API local do Jan | `http://localhost:1337/v1` |
| `WHISPER_MODEL` | Tamanho do Whisper (`tiny`, `base`, `small`...) | `tiny` |
| `LOG_LEVEL` | Detalhe dos logs no terminal (`DEBUG`, `INFO`, `WARNING` ou `ERROR`) | `INFO` |

## Banco de dados

O arquivo `database/mercado.db` é criado automaticamente pelo servidor na primeira execução, com dados de exemplo (estoque de um mercado). Tabelas: `categorias`, `fornecedores`, `produtos`, `lotes` e `movimentacoes`.

O seed só roda se o arquivo **não** existir. Se o schema mudar (por exemplo, depois de um `git pull`), apague `database/mercado.db` e suba o servidor de novo.

## Dependências

Nunca instale nada com `pip install` direto no projeto. Para adicionar uma biblioteca, declare-a no `requirements.in`, recompile o lock e sincronize o ambiente:

```bash
pip-compile --unsafe-package triton requirements.in   # gera o lock travado
pip-sync requirements.txt                             # instala o que falta, remove o que sobrou
```

Commite sempre os dois arquivos (`.in` e `.txt`). Depois de um `git pull`, basta ativar o `venv` e rodar `pip-sync requirements.txt`.

## Notas

- **O servidor de IA precisa estar ativo.** Com o Local API Server do Jan desligado, toda pergunta falha.
- **O nome do modelo vem do `.env`** (`LLM_MODEL`) e deve bater com o `id` do modelo no Jan, mantendo o prefixo `openai/`. Se o modelo foi baixado como `.gguf`, acrescente `.gguf` ao final do nome.
- **Modelo local pode ser lento.** Em computadores sem GPU, uma pergunta pode levar vários minutos. O bot só responde quando o modelo termina.
- **O token do Telegram fica no `.env`**, não no código. Copie `.env.example` para `.env` e preencha `TELEGRAM_API_TOKEN`. O `.env` está no `.gitignore` e nunca deve ser commitado. Nunca cole o token em chat, print ou log.
- **Cada pessoa usa o seu próprio bot.** Duas pessoas com o mesmo token ao mesmo tempo causam conflito no Telegram.
- **O Whisper roda em CPU.** O lock usa `torch+cpu` para manter a paridade com o Windows. Quem quiser GPU precisa instalar o torch CUDA à parte, fora do `pip-sync`.
- **O Whisper exige ffmpeg** (`sudo apt install ffmpeg` / `winget install Gyan.FFmpeg` / `choco install ffmpeg`) e baixa o modelo na primeira transcrição. O modelo `tiny` pode errar palavras; `WHISPER_MODEL=base` melhora.
- **A resposta é texto**, com no máximo 20 linhas e 4000 caracteres (o Telegram aceita 4096 por mensagem).
- **O terminal mostra o andamento de cada pergunta**, com horário em cada linha: pergunta recebida, tempo da transcrição, tempo e SQL de cada tentativa do modelo, erros de validação, consulta ao banco e uma linha final de resumo (`resumo origem=texto tentativas=1 llm_s=53.2 sql_s=0.004 linhas=1 total_s=53.3`). Com `LOG_LEVEL=DEBUG` sai também o raciocínio do modelo.
- **A linha de resumo tem nível próprio** (`bot.metricas`), então ela aparece em qualquer `LOG_LEVEL`. Com `WARNING` ficam só ela e os avisos; com `ERROR`, só ela. É assim que se roda muitas perguntas de uma vez sem encher o terminal.
- **O SQL gerado é validado antes de ir para o banco.** O bot monta um SQLite em memória com o mesmo schema e compila a consulta ali. Se o modelo errar, ele recebe o erro de volta e tenta de novo, até 3 vezes; depois disso o bot pede para reformular a pergunta. O terminal mostra o SQL e a tentativa de cada pergunta.