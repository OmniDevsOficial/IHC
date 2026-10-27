# ======================================================================
# 1. CONFIGURAÇÃO
# ======================================================================

import dspy
import logging
import os
import re
import sqlite3
import tempfile
import time
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

# Quantas vezes o gerador tenta de novo quando o SQL sai inválido
MAX_TENTATIVAS = 3


# Logs no terminal: cada linha sai com horário, nível e origem.
# LOG_LEVEL=DEBUG no .env mostra também o raciocínio do modelo em cada tentativa.
FORMATO_LOG = "%(asctime)s %(levelname)s %(name)s %(message)s"
HORA_LOG = "%H:%M:%S"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format=FORMATO_LOG,
    datefmt=HORA_LOG,
)

# Bibliotecas que falam demais no terminal: só avisos e erros
for _nome_logger in ("LiteLLM", "httpx", "httpcore", "urllib3"):
    logging.getLogger(_nome_logger).setLevel(logging.WARNING)

telebot.logger.setLevel(logging.INFO)

log = logging.getLogger("bot")

# A linha de resumo fica num logger próprio, com nível fixo. Assim as etapas 5 e 6
# podem rodar em lote com LOG_LEVEL=ERROR, sem o passo a passo de cada pergunta,
# e ainda receber uma linha de números por pergunta.
metricas = logging.getLogger("bot.metricas")
metricas.setLevel(logging.INFO)


# ======================================================================
# 2. SCHEMA E VALIDAÇÃO DO SQL
# ======================================================================

class ConsultaInvalida(Exception):
    """O modelo não produziu um SQL válido dentro do limite de tentativas."""

    def __init__(self, historico: str, tentativas: int = 0):
        super().__init__(historico)
        self.tentativas = tentativas


# Cache do schema (buscado no servidor na primeira pergunta)
_schema_cache = None


def obter_schema() -> dict:
    """Descrição do schema e DDL das tabelas, buscados no servidor uma única vez.

    O schema mora no server.py. Como o bot não importa o servidor, ele pega as
    duas coisas pela rota: a descrição alimenta o modelo e o DDL monta o banco
    em memória usado na validação.
    """
    global _schema_cache
    if _schema_cache is None:
        inicio = time.perf_counter()
        resposta = requests.get(
            f"{SERVER_URL}/query",
            params={"schema": "true"},
            timeout=15,
        )
        resposta.raise_for_status()
        dados = resposta.json()
        _schema_cache = {"descricao": dados["descricao"], "ddl": dados.get("ddl", [])}
        log.info(
            "schema carregado do servidor em %.2fs (%d tabela(s) no DDL)",
            time.perf_counter() - inicio,
            len(_schema_cache["ddl"]),
        )
    return _schema_cache


def limpar_sql(sql: str) -> str:
    """Deixa só a primeira instrução SQL, sem cerca de markdown e sem ponto e vírgula."""
    texto = sql.strip()

    # O modelo às vezes devolve o SQL dentro de ```sql ... ```
    # (problema 2 do problemas_percebidos.txt)
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-zA-Z]*\s*", "", texto)
        texto = re.sub(r"```\s*$", "", texto)

    # O dspy fecha a resposta com [[ ## completed ## ]] e o modelo às vezes
    # escreve esse marcador (ou uma variação dele) grudado no fim do SQL
    texto = texto.split("[[", 1)[0]

    texto = texto.strip()

    # complete_statement respeita texto entre aspas e comentários, então cortar
    # aqui não quebra uma consulta que tenha ponto e vírgula dentro de um valor
    acumulado = ""
    for caractere in texto:
        acumulado += caractere
        if sqlite3.complete_statement(acumulado):
            texto = acumulado
            break

    return texto.strip().rstrip(";").strip()


def validar_sql(sql: str, ddl) -> None:
    """Compila o SQL num SQLite em memória com o mesmo schema do banco real.

    O banco de verdade não é tocado: o EXPLAIN apenas prepara a consulta, o que
    já pega erro de sintaxe, tabela ou coluna inexistente e nome ambíguo.
    Levanta sqlite3.Error quando o SQL não presta.
    """
    if not sql:
        raise sqlite3.ProgrammingError("o modelo não devolveu nenhum SQL")

    conexao = sqlite3.connect(":memory:")
    try:
        for comando in ddl:
            conexao.execute(comando)
        conexao.execute(f"EXPLAIN {sql}")
    finally:
        conexao.close()


# ======================================================================
# 3. LLM / GERADOR DE SQL
# ======================================================================

lm = dspy.LM(LLM_MODEL, api_base=LLM_API_BASE, api_key='not-needed')
dspy.configure(lm=lm)


class TextToSQL(dspy.Signature):
    """Generate a SQLite query from a natural language question (in Portuguese).
    Use only the tables and columns described in dbschema.
    For calculated columns (SUM, COUNT, AVG, etc.), always give a readable alias
    in Portuguese snake_case, e.g. SUM(estoque * preco) AS total_em_reais.
    When erro_anterior is not empty, it contains the queries you already tried
    and the errors they raised: write a different query that fixes them, never
    the same one again.
    Return only the SQL query, without comments."""
    dbschema = dspy.InputField(desc="Databases schema")
    question = dspy.InputField(desc="Natural language question")
    erro_anterior = dspy.InputField(desc="Queries already tried and their errors, empty on the first attempt")

    sql_query = dspy.OutputField(desc="Valid SQL query")


class ReliableSQLGenerator(dspy.Module):
    def __init__(self):
        super().__init__()
        self.generate_sql = dspy.ChainOfThought(TextToSQL)

    def forward(self, dbschema, question, ddl=()):
        # O histórico inteiro vai de volta para o modelo. Além de dar mais
        # contexto, isso muda o prompt a cada tentativa: com o prompt repetido,
        # o cache do DSPy devolveria a mesma resposta e a tentativa seria perdida.
        erros = []
        ja_tentados = []

        for tentativa in range(1, MAX_TENTATIVAS + 1):
            log.info("tentativa %d/%d: pedindo o SQL ao modelo...", tentativa, MAX_TENTATIVAS)
            inicio = time.perf_counter()
            pred = self.generate_sql(
                dbschema=dbschema,
                question=question,
                erro_anterior="\n\n".join(erros),
            )
            duracao = time.perf_counter() - inicio
            sql = limpar_sql(pred.sql_query)
            log.info("tentativa %d/%d respondeu em %.1fs: %s", tentativa, MAX_TENTATIVAS, duracao, sql)
            log.debug("raciocínio do modelo: %s", getattr(pred, "reasoning", ""))

            # A etapa 4 (checagem de só SELECT) entra aqui, sobre o SQL já limpo

            try:
                validar_sql(sql, ddl)
            except sqlite3.Error as e:
                log.warning("tentativa %d/%d inválida: %s", tentativa, MAX_TENTATIVAS, e)
                detalhe = f"Tentativa {tentativa}:\nSQL: {sql}\nErro do SQLite: {e}"

                if sql in ja_tentados:
                    log.warning("o modelo repetiu um SQL já recusado, então paro as tentativas aqui")
                    raise ConsultaInvalida("\n\n".join(erros + [detalhe]), tentativa)

                ja_tentados.append(sql)
                erros.append(detalhe)
                continue

            return dspy.Prediction(sql_query=sql, tentativas=tentativa)

        raise ConsultaInvalida("\n\n".join(erros), MAX_TENTATIVAS)


# Gerador criado uma única vez e reaproveitado em todas as perguntas
generator = ReliableSQLGenerator()


# ======================================================================
# 4. CONSULTA AO SERVIDOR
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
        log.error("consulta ao servidor falhou: %s", e)
        return {"erro": f"Não foi possível consultar o servidor: {e}"}


# ======================================================================
# 5. RESPOSTA AO USUÁRIO
# ======================================================================

def _formatar_valor(valor) -> str:
    if valor is None:
        return "sem valor"
    if isinstance(valor, float):
        return f"{valor:.2f}"
    return str(valor)


def formatar_resposta(resultado) -> str:
    """Transforma o resultado da consulta em texto legível para o Telegram."""
    if isinstance(resultado, dict) and "erro" in resultado:
        return f"Ops: {resultado['erro']}"

    if not resultado:
        return "Não encontrei nenhum resultado para essa pergunta."

    # SUM, AVG e afins devolvem uma linha com tudo nulo quando nada bate com o
    # filtro. Sem isso, a resposta viraria algo como "total_em_reais: sem valor"
    if len(resultado) == 1 and all(valor is None for valor in resultado[0].values()):
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


def _logar_resumo(**medidas) -> None:
    """Uma linha por pergunta, com os números que a etapa 5 vai querer medir."""
    metricas.info("resumo %s", " ".join(f"{chave}={valor}" for chave, valor in medidas.items() if valor is not None))


def responder_pergunta(pergunta: str, origem: str = "texto", transcricao_s=None) -> str:
    """Fluxo completo: pergunta em português -> SQL validado -> consulta -> texto de resposta."""
    inicio_total = time.perf_counter()
    log.info("pergunta por %s: %s", origem, pergunta.strip())

    transcricao = f"{transcricao_s:.1f}" if transcricao_s is not None else None

    try:
        schema = obter_schema()
    except requests.exceptions.RequestException as e:
        log.error("não foi possível buscar o schema no servidor: %s", e)
        _logar_resumo(
            origem=origem,
            transcricao_s=transcricao,
            status="sem_servidor",
            total_s=f"{time.perf_counter() - inicio_total:.1f}",
        )
        return f"Ops: não foi possível buscar o schema no servidor: {e}"

    inicio_llm = time.perf_counter()
    try:
        pred = generator(
            dbschema=schema["descricao"],
            question=pergunta,
            ddl=schema["ddl"],
        )
    except ConsultaInvalida as e:
        log.warning("desisti de gerar o SQL. Histórico:\n%s", e)
        _logar_resumo(
            origem=origem,
            transcricao_s=transcricao,
            status="sql_invalido",
            tentativas=e.tentativas,
            llm_s=f"{time.perf_counter() - inicio_llm:.1f}",
            total_s=f"{time.perf_counter() - inicio_total:.1f}",
        )
        return (
            "Não consegui montar uma consulta válida para essa pergunta. "
            "Tente reformular, por exemplo citando o nome do produto ou da categoria."
        )
    except Exception as e:
        log.error("erro ao falar com o modelo de IA: %s", e)
        _logar_resumo(
            origem=origem,
            transcricao_s=transcricao,
            status="erro_modelo",
            llm_s=f"{time.perf_counter() - inicio_llm:.1f}",
            total_s=f"{time.perf_counter() - inicio_total:.1f}",
        )
        return (
            "Ops: não consegui falar com o modelo de IA. "
            "Confira se o Local API Server do Jan está ligado."
        )
    tempo_llm = time.perf_counter() - inicio_llm

    inicio_sql = time.perf_counter()
    resultado = execute_query_remota(pred.sql_query)
    tempo_sql = time.perf_counter() - inicio_sql
    linhas = len(resultado) if isinstance(resultado, list) else 0

    erro_na_consulta = isinstance(resultado, dict) and "erro" in resultado
    _logar_resumo(
        origem=origem,
        transcricao_s=transcricao,
        status="erro_consulta" if erro_na_consulta else "ok",
        tentativas=pred.tentativas,
        llm_s=f"{tempo_llm:.1f}",
        sql_s=f"{tempo_sql:.3f}",
        linhas=linhas,
        total_s=f"{time.perf_counter() - inicio_total:.1f}",
    )

    return formatar_resposta(resultado)


# ======================================================================
# 6. VOZ (WHISPER)
# ======================================================================

# Modelo carregado só na primeira transcrição e reaproveitado depois
_whisper_model = None


def _obter_modelo_whisper():
    global _whisper_model
    if _whisper_model is None:
        log.info("carregando o modelo do Whisper (%s), isso acontece uma vez só...", WHISPER_MODEL)
        inicio = time.perf_counter()
        _whisper_model = whisper.load_model(WHISPER_MODEL)
        log.info("Whisper pronto em %.1fs", time.perf_counter() - inicio)
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
# 7. HANDLERS DO TELEGRAM + MAIN
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
    log.info("áudio recebido (chat %s, %ss)", message.chat.id, message.voice.duration)

    # Baixamos o áudio antes de transcrever: o ffmpeg (usado pelo Whisper) não
    # consegue abrir a URL https do Telegram sozinho no Windows (falha de certificado).
    caminho_temp = None
    try:
        inicio = time.perf_counter()
        info = bot.get_file(message.voice.file_id)
        audio_bytes = bot.download_file(info.file_path)

        with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as arquivo:
            arquivo.write(audio_bytes)
            caminho_temp = arquivo.name
        log.info("áudio baixado: %d KB", len(audio_bytes) // 1024)

        texto = whisper_transcribe(caminho_temp)
        transcricao_s = time.perf_counter() - inicio
        log.info("transcrição em %.1fs: %s", transcricao_s, texto.strip())

        bot.reply_to(message, responder_pergunta(texto, origem="voz", transcricao_s=transcricao_s))
    except Exception as e:
        # Nunca mostra o token: remove-o da mensagem de erro, se aparecer
        log.error("erro ao processar áudio: %s", str(e).replace(API_TOKEN, "***"))
        bot.reply_to(message, "Ops: não consegui processar esse áudio.")
    finally:
        if caminho_temp and os.path.exists(caminho_temp):
            os.remove(caminho_temp)


def main():
    log.info("bot iniciado, consultando o backend em %s", SERVER_URL)
    bot.polling()


if __name__ == "__main__":
    main()
