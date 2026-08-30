# Text-to-SQL via Telegram — IHC

Projeto da disciplina de **Interação Humano-Computador (IHC)** — 3º DSM / Fatec.

Explora uma forma natural de interação com dados: em vez de escrever SQL, o usuário pergunta em português a um bot do Telegram — por texto ou áudio — e o sistema traduz a pergunta em consulta SQL, executa no SQLite e devolve o resultado.

```
usuário (texto ou voz) → Telegram → Whisper → DSPy + LLM local → SQL → SQLite → resposta
```

## Estrutura

```
bot.py               # bot do Telegram (Whisper + DSPy + SQLite)
server.py            # API FastAPI
requirements.in      # dependências diretas (é este que você edita)
requirements.txt     # lock gerado pelo pip-compile (não edite à mão)
```

## Setup

```bash
python -m venv .venv                  # cria o ambiente virtual
source ./.venv/Scripts/activate       # ativa (Linux: source .venv/bin/activate)
pip install pip-tools                 # instala o gerenciador de dependências
pip-sync requirements.txt             # espelha o ambiente com o lock
cp .env.example .env                  # copia o template e preenche o token (ver notas)
```

## Rodar

```bash
python bot.py                         # bot do Telegram
uvicorn server:app --reload           # API FastAPI (outro terminal)
```

## Dependências

Nunca instale nada com `pip install` direto no projeto. Para adicionar uma biblioteca, declare-a no `requirements.in`, recompile o lock e sincronize o ambiente:

```bash
pip-compile --unsafe-package triton requirements.in   # gera o lock travado
pip-sync requirements.txt                             # instala o que falta, remove o que sobrou
```

Commite sempre os dois arquivos (`.in` e `.txt`). Depois de um `git pull`, basta ativar o `.venv` e rodar `pip-sync requirements.txt`.

## Notas

- **O servidor de IA precisa estar ativo.** O modelo roda localmente via [Jan.ai](https://jan.ai/) em `http://localhost:1337/v1`. Ligue o *Local API Server* antes de executar o script; com ele desligado, toda pergunta falha.
- **O nome do modelo no código deve bater com o do Jan** ([bot.py:53](bot.py#L53)), mantendo o prefixo `openai/`. Se o modelo foi baixado como `.gguf`, acrescente `.gguf` ao final do nome.
- **O token do Telegram fica no `.env`**, não no código. Copie `.env.example` para `.env` e preencha `TELEGRAM_API_TOKEN`. Para usar seu próprio bot, gere um token com o [@BotFather](https://t.me/BotFather) (`/newbot`). O `.env` está no `.gitignore` e nunca deve ser commitado.
- **O Whisper roda em CPU.** O lock usa `torch+cpu` para manter a paridade com o Windows; com o modelo `tiny` do `bot.py` a transcrição continua viável. Quem quiser GPU precisa instalar o torch CUDA à parte, fora do `pip-sync`.
- **O Whisper exige ffmpeg** (`sudo apt install ffmpeg` / `choco install ffmpeg`) e baixa o modelo `tiny` na primeira transcrição.
- O banco `database/lojas.db` é criado automaticamente na primeira execução, com dados de exemplo.
