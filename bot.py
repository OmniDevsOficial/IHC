# ======================================================================
# 1. CONFIGURAÇÃO
# ======================================================================

import dspy
import os
import tempfile
import requests
import telebot #pip install pytelegrambotapi
import whisper #pip install -U openai-whisper
### whisper requires ffmpeg: on windows: choco install ffmpeg
from dotenv import load_dotenv

load_dotenv()

# URL do backend (server.py) rodando via uvicorn
# uvicorn server:app --reload   -> sobe em http://127.0.0.1:8000
SERVER_URL = os.getenv("SERVER_URL", "http://127.0.0.1:8000")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gemma-4-E2B-it-IQ4_XS")
LLM_API_BASE = os.getenv("LLM_API_BASE", "http://localhost:1337/v1")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "tiny")

API_TOKEN = os.getenv("TELEGRAM_API_TOKEN")
if not API_TOKEN:
    raise RuntimeError("TELEGRAM_API_TOKEN não definido. Crie um arquivo .env com base em .env.example")

# Limites da resposta enviada ao usuário
MAX_LINHAS = 20          # máximo de registros mostrados
LIMITE_TELEGRAM = 4000   # o Telegram aceita 4096 caracteres por mensagem


# ======================================================================
# 2. LLM / GERADOR DE SQL
# ======================================================================

lm = dspy.LM(LLM_MODEL, api_base=LLM_API_BASE, api_key='not-needed')
dspy.configure(lm=lm)


class TextToSQL(dspy.Signature):
    """Generate a SQLite query from a natural language question (in Portuguese).
    Use only the tables and columns described in dbschema.
    For calculated columns (SUM, COUNT, AVG, etc.), always give a readable alias
    in Portuguese snake_case, e.g. SUM(estoque * preco) AS total_em_reais.
    Return only the SQL query, without comments."""
    dbschema = dspy.InputField(desc="Databases schema")
    question = dspy.InputField(desc="Natural language question")

    sql_query = dspy.OutputField(desc="Valid SQL query")


class ReliableSQLGenerator(dspy.Module):
    def __init__(self):
        super().__init__()
        self.generate_sql = dspy.ChainOfThought(TextToSQL)

    def forward(self, dbschema, question):
        # As etapas 3 e 4 (validação e correção do SQL) entram aqui, depois da geração
        pred = self.generate_sql(dbschema=dbschema, question=question)
        return pred


# Gerador criado uma única vez e reaproveitado em todas as perguntas
generator = ReliableSQLGenerator()

# Cache do schema (buscado no servidor na primeira pergunta)
_schema_cache = None


def obter_schema() -> str:
    # O schema mora no server.py; o bot busca uma vez e guarda em cache
    global _schema_cache
    if _schema_cache is None:
        resposta = requests.get(
            f"{SERVER_URL}/query",
            params={"schema": "true"},
            timeout=15,
        )
        resposta.raise_for_status()
        _schema_cache = resposta.json()["descricao"]
    return _schema_cache


# ======================================================================
# 3. CONSULTA AO SERVIDOR
# ======================================================================

def execute_query_remota(sql: str):
    try:
        resposta = requests.get(
            f"{SERVER_URL}/query",
            params={"sql": sql},
            timeout=15,
        )
        resposta.raise_for_status()
        dados = resposta.json()
        return dados.get("resultado", dados)
    except requests.exceptions.RequestException as e:
        return {"erro": f"Não foi possível consultar o servidor: {e}"}


# ======================================================================
# 4. RESPOSTA AO USUÁRIO
# ======================================================================

def _formatar_valor(valor) -> str:
    if valor is None:
        return "—"
    if isinstance(valor, float):
        return f"{valor:.2f}"
    return str(valor)


def formatar_resposta(resultado) -> str:
    """Transforma o resultado da consulta em texto legível para o Telegram."""
    if isinstance(resultado, dict) and "erro" in resultado:
        return f"Ops: {resultado['erro']}"

    if not resultado:
        return "Não encontrei nenhum resultado para essa pergunta."

    linhas = []
    for registro in resultado[:MAX_LINHAS]:
        campos = " | ".join(f"{coluna}: {_formatar_valor(valor)}" for coluna, valor in registro.items())
        linhas.append(f"• {campos}")

    restantes = len(resultado) - MAX_LINHAS
    if restantes > 0:
        linhas.append(f"… e mais {restantes} resultado(s).")

    texto = "\n".join(linhas)
    if len(texto) > LIMITE_TELEGRAM:
        texto = texto[:LIMITE_TELEGRAM] + "\n… (resposta cortada)"
    return texto


def responder_pergunta(pergunta: str) -> str:
    """Fluxo completo: pergunta em português -> SQL -> consulta -> texto de resposta."""
    try:
        schema = obter_schema()
    except requests.exceptions.RequestException as e:
        return f"Ops: não foi possível buscar o schema no servidor: {e}"

    pred = generator(dbschema=schema, question=pergunta)
    print(f"Pergunta: {pergunta}\nSQL: {pred.sql_query}")

    resultado = execute_query_remota(pred.sql_query)
    return formatar_resposta(resultado)


# ======================================================================
# 5. VOZ (WHISPER)
# ======================================================================

# Modelo carregado só na primeira transcrição e reaproveitado depois
_whisper_model = None


def _obter_modelo_whisper():
    global _whisper_model
    if _whisper_model is None:
        _whisper_model = whisper.load_model(WHISPER_MODEL)
    return _whisper_model


def whisper_transcribe(filepath: str) -> str:
    """
    Function to perform ASR on an audio file (path or URL).
    :param filepath: Path (or URL) to the audio file.
    :return: transcribed audio.
    """
    result = _obter_modelo_whisper().transcribe(filepath)

    return result["text"]


# ======================================================================
# 6. HANDLERS DO TELEGRAM + MAIN
# ======================================================================

bot = telebot.TeleBot(API_TOKEN)


@bot.message_handler(commands=['start', 'help'])
def handle_commands(message):
    bot.reply_to(
        message,
        "Oi! Pode me perguntar algo sobre os produtos do mercado, "
        "por texto ou áudio. Ex: 'qual o preço do sabonete?'"
    )


@bot.message_handler(func=lambda message: message.text and not message.text.startswith("/"))
def handle_texto(message):
    bot.reply_to(message, responder_pergunta(message.text))


@bot.message_handler(content_types=['voice'])
def handle_voz(message):
    # Baixamos o áudio antes de transcrever: o ffmpeg (usado pelo Whisper) não
    # consegue abrir a URL https do Telegram sozinho no Windows (falha de certificado).
    caminho_temp = None
    try:
        info = bot.get_file(message.voice.file_id)
        audio_bytes = bot.download_file(info.file_path)

        with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as arquivo:
            arquivo.write(audio_bytes)
            caminho_temp = arquivo.name

        texto = whisper_transcribe(caminho_temp)
        bot.reply_to(message, responder_pergunta(texto))
    except Exception as e:
        # Nunca imprime o token: remove-o da mensagem de erro, se aparecer
        print(f"Erro ao processar áudio: {str(e).replace(API_TOKEN, '***')}")
        bot.reply_to(message, "Ops: não consegui processar esse áudio.")
    finally:
        if caminho_temp and os.path.exists(caminho_temp):
            os.remove(caminho_temp)


def main():
    print(f"Bot iniciado. Consultando o backend em {SERVER_URL}")
    bot.polling()


if __name__ == "__main__":
    main()