# Text-to-SQL via Telegram — IHC

Projeto da disciplina de **Interação Humano-Computador (IHC)** — 3º DSM / Fatec.

Explora uma forma natural de interação com dados: em vez de escrever SQL, o usuário pergunta em português a um bot do Telegram — por texto ou áudio — e o sistema traduz a pergunta em consulta SQL, executa no SQLite e devolve o resultado.

```
usuário (texto ou voz) → Telegram → Whisper → DSPy + LLM local → SQL → SQLite → resposta
```

## Comandos

```bash
python3 -m venv venv              # cria o ambiente virtual
source venv/bin/activate          # ativa (Windows: venv\Scripts\activate)
pip install -r requirements.txt   # instala as dependências
cp .env.example .env              # copia o template e preenche o token (ver notas)
python text-to-sql.py             # roda o bot
```

## Notas

- **O servidor de IA precisa estar ativo.** O modelo roda localmente via [Jan.ai](https://jan.ai/) em `http://localhost:1337/v1`. Ligue o *Local API Server* antes de executar o script; com ele desligado, toda pergunta falha.
- **O nome do modelo no código deve bater com o do Jan** ([text-to-sql.py:46](text-to-sql.py#L46)), mantendo o prefixo `openai/`. Se o modelo foi baixado como `.gguf`, acrescente `.gguf` ao final do nome.
- **O token do Telegram fica no `.env`**, não no código. Copie `.env.example` para `.env` e preencha `TELEGRAM_API_TOKEN`. Para usar seu próprio bot, gere um token com o [@BotFather](https://t.me/BotFather) (`/newbot`). O `.env` está no `.gitignore` e nunca deve ser commitado.
- **O Whisper exige ffmpeg** (`sudo apt install ffmpeg` / `choco install ffmpeg`) e baixa o modelo `tiny` na primeira transcrição.
- O banco `database/lojas.db` é criado automaticamente na primeira execução, com dados de exemplo.
