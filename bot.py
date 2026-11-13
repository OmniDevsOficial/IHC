# ======================================================================
# 1. CONFIGURAÇÃO
# ======================================================================

import argparse
import dspy
import itertools
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from collections import Counter
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


def separar_instrucoes(sql: str) -> list[str]:
    """Separa a resposta do modelo em instruções SQL, sem cerca de markdown e sem ponto e vírgula.

    A primeira instrução é a consulta que vai para o banco. As outras nunca são
    enviadas, mas a validação confere se alguma delas é escrita.
    """
    texto = sql.strip()

    # O modelo às vezes devolve o SQL dentro de ```sql ... ```
    # (problema 2 do problemas_percebidos.txt)
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-zA-Z]*\s*", "", texto)
        texto = re.sub(r"```\s*$", "", texto)

    # O dspy fecha a resposta com [[ ## completed ## ]] e o modelo às vezes
    # escreve esse marcador (ou uma variação dele) grudado no fim do SQL
    texto = texto.split("[[", 1)[0]

    # complete_statement respeita texto entre aspas e comentários, então separar
    # aqui não quebra uma consulta que tenha ponto e vírgula dentro de um valor
    instrucoes = []
    acumulado = ""
    for caractere in texto:
        acumulado += caractere
        if sqlite3.complete_statement(acumulado):
            instrucoes.append(acumulado)
            acumulado = ""
    instrucoes.append(acumulado)

    limpas = (instrucao.strip().rstrip(";").strip() for instrucao in instrucoes)
    return [instrucao for instrucao in limpas if instrucao]


# Ações que uma consulta de leitura pede ao SQLite. O autorizador nega todas as
# outras, então um comando que ninguém lembrou de listar também fica bloqueado.
ACOES_LEITURA = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}

MENSAGEM_BLOQUEIO = "Bloqueado: Apenas consultas (SELECT) são permitidas."


def _compilar_so_leitura(conn, sql: str) -> None:
    """Compila o SQL com EXPLAIN, liberando só as ações de leitura.

    Levanta PermissionError quando o SQL não é leitura e sqlite3.Error quando
    ele não compila.
    """
    acoes = []

    def autorizar(acao, *args):
        acoes.append(acao)
        return sqlite3.SQLITE_OK if acao in ACOES_LEITURA else sqlite3.SQLITE_DENY

    conn.set_authorizer(autorizar)
    try:
        conn.execute(f"EXPLAIN {sql}")
    except sqlite3.DatabaseError as e:
        if any(acao not in ACOES_LEITURA for acao in acoes):
            raise PermissionError(MENSAGEM_BLOQUEIO) from e
        raise

    # Alguns comandos, como o VACUUM, nem passam pelo autorizador. Toda leitura
    # passa por SQLITE_SELECT, então o que não passou não é consulta
    if sqlite3.SQLITE_SELECT not in acoes:
        raise PermissionError(MENSAGEM_BLOQUEIO)


def validar_sql(sql: str, ddl_schema, extras=()) -> None:
    """Compila o SQL num SQLite em memória com o mesmo schema do banco real.

    O banco de verdade não é tocado: o EXPLAIN apenas prepara a consulta, o que
    já pega erro de sintaxe, tabela ou coluna inexistente e nome ambíguo.
    O autorizador garante que só consultas de leitura passem.
    `extras` são as instruções que o modelo escreveu depois da primeira: elas
    não vão para o banco, mas se alguma for escrita o pedido inteiro é bloqueado.
    Levanta PermissionError quando há escrita ou alteração e sqlite3.Error
    quando o SQL não presta.
    """
    if not sql:
        raise sqlite3.ProgrammingError("o modelo não devolveu nenhum SQL")

    conn = sqlite3.connect(":memory:")
    try:
        for comando in ddl_schema:
            conn.execute(comando)

        # As extras vêm antes para que a escrita tenha prioridade sobre um erro
        # na consulta. Extra que não compila é texto solto do modelo e é ignorada
        for extra in extras:
            try:
                _compilar_so_leitura(conn, extra)
            except sqlite3.Error:
                pass

        _compilar_so_leitura(conn, sql)
    finally:
        conn.close()


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

    def forward(self, dbschema, question, ddl=(), validar: bool = True):
        # Com validar=False (CoT original, sem as etapas 3 e 4), faz uma única
        # chamada ao modelo, sem limpar nem validar o SQL gerado.
        if not validar:
            pred = self.generate_sql(
                dbschema=dbschema,
                question=question,
                erro_anterior="",
            )
            return dspy.Prediction(sql_query=pred.sql_query, tentativas=1)

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
            instrucoes = separar_instrucoes(pred.sql_query)
            sql = instrucoes[0] if instrucoes else ""
            extras = instrucoes[1:]
            log.info("tentativa %d/%d respondeu em %.1fs: %s", tentativa, MAX_TENTATIVAS, duracao, sql)
            if extras:
                log.info("tentativa %d/%d trouxe instruções a mais, que não vão para o banco: %s",
                         tentativa, MAX_TENTATIVAS, "; ".join(extras))
            log.debug("raciocínio do modelo: %s", getattr(pred, "reasoning", ""))

            try:
                validar_sql(sql, ddl, extras)
            except PermissionError as e:
                log.warning("tentativa %d/%d bloqueada: %s (SQL: %s)",
                            tentativa, MAX_TENTATIVAS, e, "; ".join(instrucoes))
                return dspy.Prediction(
                    sql_query=None,
                    erro=str(e),
                    tentativas=tentativa,
                )
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

    if getattr(pred, "erro", None):
        _logar_resumo(
            origem=origem,
            transcricao_s=transcricao,
            status="sql_bloqueado",
            tentativas=pred.tentativas,
            llm_s=f"{tempo_llm:.1f}",
            total_s=f"{time.perf_counter() - inicio_total:.1f}",
        )
        return pred.erro

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
# 7. HANDLERS DO TELEGRAM
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


# ======================================================================
# 8. AVALIAÇÃO (ETAPA 5)
# ======================================================================
# `python bot.py --avaliar` roda o eval/dataset.json sem subir o Telegram.
# Precisa do server.py e do Jan ligados, igual ao bot: o schema e a execução
# dos SQLs passam pela mesma rota /query que o usuário usa.

PASTA_EVAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval")
CAMINHO_DATASET = os.path.join(PASTA_EVAL, "dataset.json")
PASTA_RESULTADOS = os.path.join(PASTA_EVAL, "resultados")


class ErroNaConsulta(Exception):
    """O servidor recusou ou não conseguiu executar o SQL."""


def normalizar_linha(valores) -> tuple:
    """Arredonda os floats para a comparação não depender de casas decimais."""
    return tuple(round(valor, 2) if isinstance(valor, float) else valor for valor in valores)


def executar_para_avaliar(sql: str) -> list[tuple]:
    """Executa o SQL pela rota do servidor e devolve as linhas como tuplas, na ordem das colunas.

    Levanta ErroNaConsulta quando o SQL não presta. Servidor desligado levanta
    a exceção do requests, para a avaliação parar em vez de contar tudo como erro.
    """
    if not sql or not sql.strip():
        raise ErroNaConsulta("SQL vazio")

    resposta = requests.get(f"{SERVER_URL}/query", params={"sql": sql}, timeout=15)
    if resposta.status_code == 400:
        raise ErroNaConsulta(resposta.json().get("detail", resposta.text))
    resposta.raise_for_status()
    return [normalizar_linha(linha.values()) for linha in resposta.json()["resultado"]]


def _iguais(a: list, b: list, modo: str) -> bool:
    if modo == "ordem":
        return a == b
    if modo == "conjunto":
        return set(a) == set(b)
    return Counter(a) == Counter(b)


def mesmo_resultado(linhas_pred: list[tuple], linhas_ref: list[tuple], sql_ref: str) -> bool:
    """Confere se o resultado gerado contém o resultado da referência.

    Nomes de coluna (aliases) não importam, e colunas a mais no SQL gerado são
    aceitas: o que conta é existir, entre as colunas geradas, uma coluna com os
    valores de cada coluna da referência. A ordem das linhas só importa quando a
    referência tem ORDER BY, e linhas repetidas não contam quando ela tem DISTINCT.
    """
    if re.search(r"\bORDER\s+BY\b", sql_ref, flags=re.IGNORECASE):
        modo = "ordem"
    elif re.search(r"\bSELECT\s+DISTINCT\b", sql_ref, flags=re.IGNORECASE):
        modo = "conjunto"
    else:
        modo = "contagem"

    if not linhas_ref or not linhas_pred:
        return not linhas_ref and not linhas_pred

    n_ref = len(linhas_ref[0])
    n_pred = len(linhas_pred[0])

    def coluna(linhas, i):
        return [linha[i] for linha in linhas]

    # Para cada coluna da referência, as colunas geradas com os mesmos valores.
    # Isso corta as combinações antes de comparar as linhas inteiras
    candidatas = [
        [i for i in range(n_pred) if _iguais(coluna(linhas_pred, i), coluna(linhas_ref, j), modo)]
        for j in range(n_ref)
    ]

    for colunas in itertools.product(*candidatas):
        if len(set(colunas)) < n_ref:
            continue
        projetadas = [tuple(linha[i] for i in colunas) for linha in linhas_pred]
        if _iguais(projetadas, linhas_ref, modo):
            return True
    return False


def metrica_execucao(example, pred, trace=None, pred_name=None, pred_trace=None) -> float:
    """Execution accuracy: 1.0 se acertou, 0.0 se errou.

    Pedido de escrita acerta quando o gerador bloqueia. Consulta de leitura
    acerta quando o resultado gerado contém o da referência (mesmo_resultado).
    Aceita os cinco argumentos que o dspy.GEPA exige (etapa 6).
    """
    erro = getattr(pred, "erro", None)
    if example.tipo == "bloqueado":
        return 1.0 if erro else 0.0

    sql = getattr(pred, "sql_query", None)
    if erro or not sql:
        return 0.0

    try:
        linhas_pred = executar_para_avaliar(sql)
    except ErroNaConsulta:
        return 0.0

    # A referência foi conferida pelo eval/teste_dataset.py. Se ela falhar aqui,
    # o problema é do dataset, então a exceção não é engolida
    linhas_ref = executar_para_avaliar(example.sql_referencia)
    return 1.0 if mesmo_resultado(linhas_pred, linhas_ref, example.sql_referencia) else 0.0


def carregar_dataset() -> tuple[list, list]:
    """Lê o eval/dataset.json e devolve (trainset, devset) como dspy.Example."""
    schema = obter_schema()
    with open(CAMINHO_DATASET, encoding="utf-8") as arquivo:
        itens = json.load(arquivo)

    conjuntos = {"train": [], "dev": []}
    for item in itens:
        exemplo = dspy.Example(
            id=item["id"],
            dbschema=schema["descricao"],
            question=item["pergunta"],
            ddl=schema["ddl"],
            sql_referencia=item.get("sql_referencia"),
            tipo=item["tipo"],
            categoria=item["categoria"],
        ).with_inputs("dbschema", "question", "ddl")
        conjuntos[item["split"]].append(exemplo)

    return conjuntos["train"], conjuntos["dev"]


class GeradorAvaliado(dspy.Module):
    """Envolve o gerador para o dspy.Evaluate: mede o tempo e devolve SQL inválido como predição."""

    def __init__(self, validar: bool = True):
        super().__init__()
        self.gerador = generator
        self.validar = validar

    def forward(self, dbschema, question, ddl=()):
        inicio = time.perf_counter()
        try:
            pred = self.gerador(dbschema=dbschema, question=question, ddl=ddl, validar=self.validar)
        except ConsultaInvalida as e:
            return dspy.Prediction(
                sql_query=None,
                erro=None,
                tentativas=e.tentativas,
                status="sql_invalido",
                latencia_s=time.perf_counter() - inicio,
            )

        erro = getattr(pred, "erro", None)
        return dspy.Prediction(
            sql_query=pred.sql_query,
            erro=erro,
            tentativas=pred.tentativas,
            status="bloqueado" if erro else "ok",
            latencia_s=time.perf_counter() - inicio,
        )


def _media(valores: list) -> float | None:
    return sum(valores) / len(valores) if valores else None


def _resumir(resultados, nome: str, split: str, validar: bool) -> dict:
    registros = []
    for exemplo, pred, nota in resultados:
        # Predição vazia é exemplo em que o dspy.Evaluate pegou uma exceção (Jan fora do ar, por exemplo)
        status = getattr(pred, "status", None) or "erro_modelo"
        sql = getattr(pred, "sql_query", None)

        sql_valido = False
        if exemplo.tipo == "leitura" and status == "ok":
            try:
                executar_para_avaliar(sql)
                sql_valido = True
            except ErroNaConsulta:
                pass

        latencia = getattr(pred, "latencia_s", None)
        registros.append({
            "id": exemplo.id,
            "tipo": exemplo.tipo,
            "categoria": exemplo.categoria,
            "pergunta": exemplo.question,
            "sql_referencia": exemplo.sql_referencia,
            "sql_gerado": sql,
            "status": status,
            "tentativas": getattr(pred, "tentativas", None),
            "sql_valido": sql_valido,
            "acertou": nota,
            "latencia_s": round(latencia, 2) if latencia is not None else None,
        })

    leituras = [r for r in registros if r["tipo"] == "leitura"]
    bloqueados = [r for r in registros if r["tipo"] == "bloqueado"]

    def arredondar(valor, casas=4):
        return round(valor, casas) if valor is not None else None

    return {
        "nome": nome,
        "split": split,
        "validacao_ativa": validar,
        "exemplos": len(registros),
        "leituras": len(leituras),
        "bloqueados": len(bloqueados),
        "execution_accuracy": arredondar(_media([r["acertou"] for r in leituras])),
        "taxa_sql_valido": arredondar(_media([r["sql_valido"] for r in leituras])),
        # Como na 5.1: tentativas até chegar num SQL válido, só nas leituras que chegaram
        "tentativas_ate_valido": arredondar(_media([r["tentativas"] for r in leituras if r["sql_valido"]]), 2),
        "taxa_bloqueio_correto": arredondar(_media([r["acertou"] for r in bloqueados])),
        "latencia_media_s": arredondar(_media([r["latencia_s"] for r in registros if r["latencia_s"] is not None]), 2),
        "registros": registros,
    }


def _porcentagem(valor) -> str:
    return f"{valor * 100:.1f}%" if valor is not None else "n/a"


def avaliar(split: str = "dev", validar: bool = True, limite: int | None = None) -> int:
    """Roda o dspy.Evaluate no split escolhido, imprime o resumo e salva em eval/resultados/."""
    # Sem cache: com ele, uma pergunta já feita volta na hora, o que estraga a
    # latência, e a primeira tentativa com validação reaproveitaria a resposta
    # da rodada sem validação, porque o prompt é o mesmo
    dspy.configure_cache(enable_disk_cache=False, enable_memory_cache=False)

    trainset, devset = carregar_dataset()
    exemplos = {"dev": devset, "train": trainset, "todos": trainset + devset}[split]
    if limite is not None:
        exemplos = exemplos[:limite]

    nome = "cot-validacao" if validar else "cot-original"
    if generator.generate_sql.predict.signature.instructions != TextToSQL.instructions:
        nome = "gepa"  # o prompt foi trocado por carregar_otimizado()
    log.info("avaliação %s: %d exemplo(s) do split %s", nome, len(exemplos), split)

    avaliador = dspy.Evaluate(
        devset=exemplos,
        metric=metrica_execucao,
        num_threads=1,  # o Jan atende uma pergunta por vez; em paralelo, a latência sairia errada
        display_progress=True,
    )
    resultado = avaliador(GeradorAvaliado(validar=validar))
    resumo = _resumir(resultado.results, nome, split, validar)

    print(f"\n{'id':<5}{'categoria':<18}{'status':<14}{'tent.':>6}{'tempo':>8}  acertou")
    for r in resumo["registros"]:
        tempo = f"{r['latencia_s']:.1f}s" if r["latencia_s"] is not None else "n/a"
        print(f"{r['id']:<5}{r['categoria']:<18}{r['status']:<14}{str(r['tentativas']):>6}{tempo:>8}  {r['acertou']:.0f}")

    print(f"\n=== {nome} (split {split}, {resumo['exemplos']} exemplos) ===")
    print(f"Execution accuracy       : {_porcentagem(resumo['execution_accuracy'])}")
    print(f"SQL válido               : {_porcentagem(resumo['taxa_sql_valido'])}")
    print(f"Tentativas até SQL válido: {resumo['tentativas_ate_valido']}")
    print(f"Bloqueio correto         : {_porcentagem(resumo['taxa_bloqueio_correto'])}")
    print(f"Latência média           : {resumo['latencia_media_s']}s")

    os.makedirs(PASTA_RESULTADOS, exist_ok=True)
    sufixo = f"-limite{limite}" if limite is not None else ""
    caminho = os.path.join(PASTA_RESULTADOS, f"{nome}-{split}{sufixo}.json")
    with open(caminho, "w", encoding="utf-8") as arquivo:
        json.dump(resumo, arquivo, ensure_ascii=False, indent=2)
    print(f"Detalhes em {caminho}")
    return 0


# ======================================================================
# 9. OTIMIZAÇÃO COM GEPA (ETAPA 6)
# ======================================================================
# `python bot.py --otimizar` roda o dspy.GEPA no split train, mede o programa
# otimizado no dev e salva o resultado em eval/otimizado/. Precisa do server.py
# e do Jan ligados, como a avaliação.

PASTA_OTIMIZADO = os.path.join(PASTA_EVAL, "otimizado")
CAMINHO_OTIMIZADO = os.path.join(PASTA_OTIMIZADO, "gerador-gepa.json")


def _recortar(linhas: list, limite: int = 5) -> str:
    """Mostra só o começo do resultado, para o feedback não ficar enorme."""
    texto = str(linhas[:limite])
    return texto + (f" ... (+{len(linhas) - limite} linhas)" if len(linhas) > limite else "")


def metrica_gepa(example, pred, trace=None, pred_name=None, pred_trace=None):
    """Mesma nota da metrica_execucao, mais um texto dizendo o que deu errado.

    O GEPA lê o feedback para propor um prompt melhor: quanto mais específico
    ele for (SQL gerado, SQL esperado, erro do banco), melhor a proposta.
    O texto só vai para o modelo de reflexão, nunca para o gerador.
    """
    erro = getattr(pred, "erro", None)
    sql = getattr(pred, "sql_query", None)
    tentativas = getattr(pred, "tentativas", None)

    if example.tipo == "bloqueado":
        if erro:
            return dspy.Prediction(score=1.0, feedback="Pedido de escrita bloqueado corretamente.")
        return dspy.Prediction(
            score=0.0,
            feedback=(
                f"O pedido '{example.question}' pede para alterar o banco e deveria ser bloqueado, "
                f"mas o modelo devolveu o SQL: {sql}. O gerador nunca deve trocar um pedido de "
                "escrita por um SELECT inofensivo; ele deve escrever o SQL de escrita pedido, "
                "porque quem bloqueia é o validador."
            ),
        )

    if erro:
        return dspy.Prediction(
            score=0.0,
            feedback=f"Consulta de leitura bloqueada indevidamente ({erro}). SQL: {sql}",
        )
    if not sql:
        return dspy.Prediction(
            score=0.0,
            feedback=(
                f"O modelo não chegou a um SQL válido em {tentativas} tentativa(s). "
                "Use só as tabelas e colunas do schema e devolva só o SQL."
            ),
        )

    try:
        linhas_pred = executar_para_avaliar(sql)
    except ErroNaConsulta as e:
        return dspy.Prediction(score=0.0, feedback=f"O SQL gerado falhou no banco: {e}. SQL: {sql}")

    linhas_ref = executar_para_avaliar(example.sql_referencia)
    if mesmo_resultado(linhas_pred, linhas_ref, example.sql_referencia):
        return dspy.Prediction(score=1.0, feedback="Resultado igual ao esperado.")

    return dspy.Prediction(
        score=0.0,
        feedback=(
            f"Pergunta: {example.question}\n"
            f"SQL gerado: {sql}\n"
            f"Resultado gerado ({len(linhas_pred)} linha(s)): {_recortar(linhas_pred)}\n"
            f"SQL esperado: {example.sql_referencia}\n"
            f"Resultado esperado ({len(linhas_ref)} linha(s)): {_recortar(linhas_ref)}\n"
            "Compare os dois SQLs e diga qual regra geral faltou no prompt (filtro, JOIN, "
            "agregação, comparação de texto), sem copiar valores desta pergunta."
        ),
    )


def carregar_otimizado() -> None:
    """Troca o prompt do gerador pelo que o GEPA salvou. Sem o arquivo, avisa e segue com o original."""
    if not os.path.exists(CAMINHO_OTIMIZADO):
        raise SystemExit(f"Não achei {CAMINHO_OTIMIZADO}. Rode `python bot.py --otimizar` primeiro.")
    generator.load(CAMINHO_OTIMIZADO)
    log.info("gerador otimizado carregado de %s", CAMINHO_OTIMIZADO)


CHAMADAS_PADRAO = 100  # teto padrão de chamadas à métrica no GEPA


def otimizar(modelo_reflexao: str | None = None, auto: str | None = None,
             limite_chamadas: int | None = None, limite: int | None = None) -> int:
    """Roda o dspy.GEPA no train, compara com a linha de base no dev e salva o programa."""
    auto_explicito = auto is not None and limite_chamadas is None
    # Cache ligado: o GEPA repete avaliações iguais e o cache poupa chamadas ao Jan
    trainset, devset = carregar_dataset()
    if limite is not None:
        trainset, devset = trainset[:limite], devset[:limite]

    # O modelo de reflexão escreve os prompts novos. O gemma 2B local mal consegue
    # fazer isso, então o ideal é um modelo maior (REFLECTION_LM no .env)
    nome_reflexao = modelo_reflexao or os.getenv("REFLECTION_LM") or LLM_MODEL
    base_reflexao = os.getenv("REFLECTION_API_BASE", LLM_API_BASE if nome_reflexao == LLM_MODEL else None)
    chave_reflexao = os.getenv("REFLECTION_API_KEY", "not-needed")
    lm_reflexao = dspy.LM(nome_reflexao, api_base=base_reflexao, api_key=chave_reflexao,
                          temperature=1.0, max_tokens=4000)
    log.info("GEPA: gerador %s, reflexão %s, %d exemplo(s) no train", LLM_MODEL, nome_reflexao, len(trainset))

    # Durante a otimização o gerador roda com a validação ligada, igual ao bot
    programa = GeradorAvaliado(validar=True)

    # Linha de base no dev, para o antes e depois serem medidos com a mesma régua
    avaliador = dspy.Evaluate(devset=devset, metric=metrica_execucao, num_threads=1, display_progress=True)
    base = avaliador(programa)

    # O bloqueio é feito pelo validador, não pelo prompt. Se os pedidos de escrita
    # entrassem no GEPA, a métrica premiaria o modelo por escrever DELETE e ALTER.
    # Eles ficam só na avaliação (dev completo, antes e depois).
    treino_gepa = [e for e in trainset if e.tipo == "leitura"]
    valida_gepa = [e for e in devset if e.tipo == "leitura"]
    log.info("GEPA: %d leitura(s) no treino e %d na validação (pedidos de escrita ficam de fora)",
             len(treino_gepa), len(valida_gepa))

    # Teto de chamadas à métrica para a rodada caber em ~1 hora. O auto="light"
    # faz centenas e, num modelo lento, passa de 3 horas. --auto libera a busca maior.
    if auto_explicito:
        argumentos = {"auto": auto}
    else:
        argumentos = {"max_metric_calls": limite_chamadas or CHAMADAS_PADRAO}
    otimizador = dspy.GEPA(
        metric=metrica_gepa,
        reflection_lm=lm_reflexao,
        num_threads=1,  # o Jan atende uma pergunta por vez
        seed=0,
        track_stats=True,
        log_dir=os.path.join(PASTA_OTIMIZADO, "log-gepa"),
        **argumentos,
    )
    # O valset é o próprio dev: sem um teste separado, o ganho no dev está otimista
    otimizado = otimizador.compile(programa, trainset=treino_gepa, valset=valida_gepa)

    final = avaliador(otimizado)
    print(f"\nDev antes : {base.score:.1f}%")
    print(f"Dev depois: {final.score:.1f}%")

    os.makedirs(PASTA_OTIMIZADO, exist_ok=True)
    # Salva só o gerador (sem o invólucro da avaliação), que é o que o bot carrega
    otimizado.gerador.save(CAMINHO_OTIMIZADO)
    for nome, predictor in otimizado.named_predictors():
        print(f"\n--- instrução otimizada ({nome}) ---\n{predictor.signature.instructions}")
    print(f"\nPrograma salvo em {CAMINHO_OTIMIZADO}")
    return 0


# ======================================================================
# 10. MAIN
# ======================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Bot de Text-to-SQL no Telegram")
    parser.add_argument("--avaliar", action="store_true",
                        help="roda o eval/dataset.json (etapa 5) em vez de subir o bot")
    parser.add_argument("--split", choices=("dev", "train", "todos"), default="dev",
                        help="com --avaliar: conjunto de exemplos (padrão: dev)")
    parser.add_argument("--sem-validacao", action="store_true",
                        help="com --avaliar: mede o CoT original, sem limpeza, validação e checagem de SELECT")
    parser.add_argument("--limite", type=int,
                        help="com --avaliar: usa só os N primeiros exemplos do split")
    parser.add_argument("--otimizar", action="store_true",
                        help="roda o GEPA (etapa 6) no train e salva o gerador em eval/otimizado/")
    parser.add_argument("--auto", choices=("light", "medium", "heavy"),
                        help="com --otimizar: busca do GEPA por tamanho (light faz centenas de chamadas)")
    parser.add_argument("--max-chamadas", type=int,
                        help=f"com --otimizar: teto de chamadas à métrica (padrão: {CHAMADAS_PADRAO})")
    parser.add_argument("--otimizado", action="store_true",
                        help="com --avaliar: usa o gerador salvo em eval/otimizado/")
    args = parser.parse_args()

    if args.otimizar:
        return otimizar(auto=args.auto, limite_chamadas=args.max_chamadas, limite=args.limite)

    if args.otimizado:
        carregar_otimizado()

    if args.avaliar:
        return avaliar(args.split, validar=not args.sem_validacao, limite=args.limite)

    log.info("bot iniciado, consultando o backend em %s", SERVER_URL)
    bot.polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
