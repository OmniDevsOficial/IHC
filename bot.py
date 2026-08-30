import dspy
import os
import telebot #pip install pytelegrambotapi
import whisper #pip install -U openai-whisper
### whisper requires ffmpeg: on windows: choco install ffmpeg
import json
from dotenv import load_dotenv
from server import initialize_db, get_schema, execute_query # import db functions from server.py

load_dotenv()

initialize_db()

# Note: if you downloaded the gemma model as a .gguf
# you will need to add ".gguf" at the end of the AI name below
lm = dspy.LM('openai/gemma-4-E2B-it-IQ4_XS', api_base='http://localhost:1337/v1', api_key='not-needed')
dspy.configure(lm=lm)

class TextToSQL(dspy.Signature):
    """Generate SQL from natural language.

        Database schema:
          - produtos: nome, departamento
    """
    dbschema = dspy.InputField(desc="Databases schema")
    question = dspy.InputField(desc="Natural language question")

    sql_query = dspy.OutputField(desc="Valid SQL query")

class ReliableSQLGenerator(dspy.Module):
    def __init__(self):
        super().__init__()
        self.generate_sql = dspy.ChainOfThought(TextToSQL)

    def forward(self, schema, question):
        pred = self.generate_sql(schema=schema, question=question)
        return pred
    
# Example question for Telegram = "qual o departamento do sabonete?"

def generate(question):
    generator = ReliableSQLGenerator()
    schema_db = get_schema()
    sql = generator.forward(schema_db, question)
    print(f"{sql}\n{sql.sql_query}")
    results = execute_query(sql.sql_query)
    return results

# API TOKEN do bot, lido do arquivo .env (ver .env.example)
API_TOKEN = os.getenv("TELEGRAM_API_TOKEN")
if not API_TOKEN:
    raise RuntimeError("TELEGRAM_API_TOKEN não definido. Crie um arquivo .env com base em .env.example")
bot = telebot.TeleBot(API_TOKEN)

@bot.message_handler(func=lambda message: True)
def reply_hi(message):
  result = generate(message.text)               # raw SQL result transformed into raw JSON
  bot.reply_to(message, json.dumps(result))     # sends the raw generated JSON back to Telegram

@bot.message_handler(content_types=['voice'])
def transcribe_voice_message(message):
    file_id = message.voice.file_id
    # Get url to audio file.
    file_path = bot.get_file_url(file_id)

    # Transcribe the audio using Whisper AI
    text = whisper_transcribe(file_path)

    result = generate(text)                     # raw SQL result transformed into raw JSON
    bot.reply_to(message, json.dumps(result))   # sends the raw generated JSON back to Telegram

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

bot.polling()
