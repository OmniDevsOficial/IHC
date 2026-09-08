import dspy
import os
import requests
import telebot #pip install pytelegrambotapi
import whisper #pip install -U openai-whisper
### whisper requires ffmpeg: on windows: choco install ffmpeg
import json
from dotenv import load_dotenv

load_dotenv()

# URL do backend (server.py) rodando via uvicorn
# uvicorn server:app --reload   -> sobe em http://127.0.0.1:8000
SERVER_URL = os.getenv("SERVER_URL", "http://127.0.0.1:8000")


lm = dspy.LM('openai/gemma-4-E2B-it-IQ4_XS', api_base='http://localhost:1337/v1', api_key='not-needed')
dspy.configure(lm=lm)

class TextToSQL(dspy.Signature):
    """Generate SQL from natural language.

        Database schema:

        categorias(
            id INTEGER PRIMARY KEY,
            nome TEXT
        )

        produtos(
            id INTEGER PRIMARY KEY,
            nome TEXT,
            preco REAL,
            estoque INTEGER,
            categoria_id INTEGER  -- referencia categorias.id
        )

        Para perguntas envolvendo a categoria de um produto, use JOIN
        entre produtos.categoria_id e categorias.id.
    """
    dbschema = dspy.InputField(desc="Databases schema")
    question = dspy.InputField(desc="Natural language question")

    sql_query = dspy.OutputField(desc="Valid SQL query")

class ReliableSQLGenerator(dspy.Module):
    def __init__(self):
        super().__init__()
        self.generate_sql = dspy.ChainOfThought(TextToSQL)

    def forward(self, dbschema, question):
        pred = self.generate_sql(dbschema=dbschema, question=question)
        return pred

# Schema é fixo aqui (server.py só expõe 1 rota GET, então o bot
# mantém sua própria cópia da descrição pra alimentar a IA)
DB_SCHEMA = TextToSQL.__doc__


def generate(question):
    generator = ReliableSQLGenerator()
    sql = generator(dbschema=DB_SCHEMA, question=question)
    print(f"{sql}\n{sql.sql_query}")
    results = execute_query_remota(sql.sql_query)
    return results


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



API_TOKEN = os.getenv("TELEGRAM_API_TOKEN")
if not API_TOKEN:
    raise RuntimeError("TELEGRAM_API_TOKEN não definido. Crie um arquivo .env com base em .env.example")
bot = telebot.TeleBot(API_TOKEN)

@bot.message_handler(commands=['start', 'help'])
def handle_commands(message):
    bot.reply_to(
        message,
        "Oi! Pode me perguntar algo sobre os produtos do mercado, "
        "por texto ou áudio. Ex: 'qual o preço do sabonete?'"
    )

@bot.message_handler(func=lambda message: not message.text.startswith('/'))
def reply_hi(message):
  result = generate(message.text)               # raw SQL result transformed into raw JSON
  bot.reply_to(message, json.dumps(result, ensure_ascii=False))     # sends the raw generated JSON back to Telegram

@bot.message_handler(content_types=['voice'])
def transcribe_voice_message(message):
    file_id = message.voice.file_id
    # Get url to audio file.
    file_path = bot.get_file_url(file_id)

    # Transcribe the audio using Whisper AI
    text = whisper_transcribe(file_path)

    result = generate(text)                     # raw SQL result transformed into raw JSON
    bot.reply_to(message, json.dumps(result, ensure_ascii=False))   # sends the raw generated JSON back to Telegram

def whisper_transcribe(filepath: str, model="tiny") -> str:
    """
    Function to perform ASR on a .mp3 file
    :param filepath: Path to the .mp3 audiofile.
    :param model: Set the model type for whisper
    ["tiny", "base", "small", "medium", "large"].
    Larger model means more parameters, higher memory requirements and
    slower speed.
    :return: transcribed audio.
    """
    # Choose tiny model for faster output.
    model = whisper.load_model(model)
    result = model.transcribe(filepath)

    return result["text"]


def main():
    print(f"Bot iniciado. Consultando o backend em {SERVER_URL}")
    bot.polling()


if __name__ == "__main__":
    main()