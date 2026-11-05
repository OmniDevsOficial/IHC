# Teste unitário do validador e bloqueio de escrita (Etapa 4.3)

import os
import sqlite3
import sys
import types
from pathlib import Path

# Garante que a raiz do projeto esteja no sys.path ao rodar `python eval/teste_bloqueio.py`
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

# Token falso apenas para permitir o import do bot.py sem ler o token real
os.environ.setdefault("TELEGRAM_API_TOKEN", "token-falso-so-para-teste")


def _garantir_stubs_se_sem_venv() -> None:
    """Cria stubs mínimos caso o script rode fora do venv (sem alterar o comportamento no venv)."""
    try:
        import dotenv  # noqa: F401
    except ModuleNotFoundError:
        mod_dotenv = types.ModuleType("dotenv")
        mod_dotenv.load_dotenv = lambda *a, **kw: None
        sys.modules["dotenv"] = mod_dotenv

    for nome in ("requests", "whisper"):
        try:
            __import__(nome)
        except ModuleNotFoundError:
            sys.modules[nome] = types.ModuleType(nome)

    try:
        import telebot  # noqa: F401
    except ModuleNotFoundError:
        mod_telebot = types.ModuleType("telebot")
        mod_telebot.logger = types.SimpleNamespace(setLevel=lambda *a, **kw: None)

        class _FakeTeleBot:
            def __init__(self, *a, **kw):
                pass

            def message_handler(self, *a, **kw):
                return lambda fn: fn

        mod_telebot.TeleBot = _FakeTeleBot
        sys.modules["telebot"] = mod_telebot

    try:
        import dspy  # noqa: F401
    except ModuleNotFoundError:
        mod_dspy = types.ModuleType("dspy")
        mod_dspy.LM = lambda *a, **kw: None
        mod_dspy.configure = lambda *a, **kw: None
        mod_dspy.Signature = type("Signature", (), {})
        mod_dspy.Module = type("Module", (), {})
        mod_dspy.InputField = lambda *a, **kw: None
        mod_dspy.OutputField = lambda *a, **kw: None
        mod_dspy.ChainOfThought = lambda *a, **kw: None
        mod_dspy.Prediction = lambda **kw: types.SimpleNamespace(**kw)
        sys.modules["dspy"] = mod_dspy


_garantir_stubs_se_sem_venv()

from bot import limpar_sql, validar_sql  # noqa: E402

DDL_MINIMO = [
    "CREATE TABLE produtos (id INTEGER PRIMARY KEY, nome TEXT, preco_venda REAL)"
]

CASOS = [
    ("SELECT nome FROM produtos", "PASSOU"),
    ("WITH t AS (SELECT * FROM produtos) SELECT nome FROM t", "PASSOU"),
    ("DELETE FROM produtos", "BLOQUEADO"),
    ("UPDATE produtos SET preco_venda = 0", "BLOQUEADO"),
    ("DROP TABLE produtos", "BLOQUEADO"),
    ("CREATE TABLE x (a INT)", "BLOQUEADO"),
    ("WITH t AS (SELECT 1) DELETE FROM produtos", "BLOQUEADO"),
    ("PRAGMA table_info(produtos)", "BLOQUEADO"),
    ("SELECT nome FROM produtos; DROP TABLE produtos", "PASSOU"),
    ("DELETE FROM nao_existe", "INVALIDO"),
]


def classificar_sql(sql_bruto: str) -> tuple[str, str]:
    sql_limpo = limpar_sql(sql_bruto)
    try:
        validar_sql(sql_limpo, DDL_MINIMO)
        return "PASSOU", sql_limpo
    except PermissionError:
        return "BLOQUEADO", sql_limpo
    except sqlite3.Error:
        return "INVALIDO", sql_limpo


def main() -> int:
    falhas = 0
    for sql_bruto, esperado in CASOS:
        obtido, sql_limpo = classificar_sql(sql_bruto)
        status = "OK" if obtido == esperado else "FALHOU"
        if status == "FALHOU":
            falhas += 1
        print(
            f"[{status}] esperado={esperado:<9} obtido={obtido:<9} "
            f"| entrada: {sql_bruto!r} -> sql_final: {sql_limpo!r}"
        )

    print(f"\nResumo: {len(CASOS) - falhas}/{len(CASOS)} casos passaram.")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
