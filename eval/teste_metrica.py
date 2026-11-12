# Teste da métrica de execução do bot.py sem chamar o modelo (Etapa 5)
#
# A métrica executa os SQLs pela rota do servidor. Aqui essa execução é trocada
# por uma consulta direta ao banco (modo somente leitura), então o teste roda
# sem o Jan e sem o uvicorn. Precisa do venv.

import inspect
import os
import sqlite3
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

# Token falso para importar o bot.py sem ler o real. Ele segue o formato
# id:segredo porque o telebot recusa token sem dois-pontos
os.environ.setdefault("TELEGRAM_API_TOKEN", "123456:token-falso-so-para-teste")

import dspy  # noqa: E402
import bot  # noqa: E402
import server  # noqa: E402


def executar_direto(sql: str) -> list[tuple]:
    if not sql or not sql.strip():
        raise bot.ErroNaConsulta("SQL vazio")
    conn = sqlite3.connect(f"file:{server.DB_PATH}?mode=ro", uri=True)
    try:
        return [bot.normalizar_linha(linha) for linha in conn.execute(sql).fetchall()]
    except sqlite3.Error as e:
        raise bot.ErroNaConsulta(str(e)) from e
    finally:
        conn.close()


def leitura(sql_referencia: str):
    return dspy.Example(id="t", tipo="leitura", categoria="teste", sql_referencia=sql_referencia)


def pred(sql=None, erro=None):
    return dspy.Prediction(sql_query=sql, erro=erro)


REF_CATEGORIAS = "SELECT nome FROM categorias"
REF_TOP5 = "SELECT nome, preco_venda FROM produtos ORDER BY preco_venda DESC LIMIT 5"
REF_PRECO = "SELECT preco_venda FROM produtos WHERE nome = 'Sabonete'"
REF_DISTINTO = ("SELECT DISTINCT l.localizacao FROM lotes l JOIN produtos p ON p.id = l.produto_id "
                "WHERE p.nome = 'Arroz 5kg'")
BLOQUEADO = dspy.Example(id="t", tipo="bloqueado", categoria="bloqueado", sql_referencia=None)

CASOS = [
    ("Outra ordem de linhas e outro alias", leitura(REF_CATEGORIAS),
     pred("SELECT nome AS departamento FROM categorias ORDER BY nome DESC"), 1.0),
    ("Resultado diferente", leitura(REF_CATEGORIAS),
     pred("SELECT nome FROM categorias WHERE nome = 'Bebidas'"), 0.0),
    ("Coluna inexistente", leitura(REF_CATEGORIAS),
     pred("SELECT coluna_que_nao_existe FROM categorias"), 0.0),
    ("Coluna a mais é aceita", leitura(REF_PRECO),
     pred("SELECT nome, preco_venda FROM produtos WHERE nome = 'Sabonete'"), 1.0),
    ("Colunas em outra ordem", leitura(REF_TOP5),
     pred("SELECT preco_venda AS preco, nome FROM produtos ORDER BY preco_venda DESC LIMIT 5"), 1.0),
    ("Coluna a menos falha", leitura(REF_TOP5),
     pred("SELECT nome FROM produtos ORDER BY preco_venda DESC LIMIT 5"), 0.0),
    ("ORDER BY na referência e ordem trocada", leitura(REF_TOP5),
     pred("SELECT * FROM (" + REF_TOP5 + ") ORDER BY preco_venda ASC"), 0.0),
    ("DISTINCT na referência e linhas repetidas", leitura(REF_DISTINTO),
     pred("SELECT l.localizacao FROM lotes l JOIN produtos p ON p.id = l.produto_id WHERE p.nome = 'Arroz 5kg'"), 1.0),
    ("Leitura bloqueada por engano", leitura(REF_CATEGORIAS),
     pred(erro=bot.MENSAGEM_BLOQUEIO), 0.0),
    ("Pedido de escrita bloqueado", BLOQUEADO, pred(erro=bot.MENSAGEM_BLOQUEIO), 1.0),
    ("Pedido de escrita sem bloqueio", BLOQUEADO, pred("SELECT 1"), 0.0),
]


def main() -> int:
    server.initialize_db()
    bot.executar_para_avaliar = executar_direto

    falhas = 0
    for descricao, exemplo, predicao, esperado in CASOS:
        obtido = bot.metrica_execucao(exemplo, predicao)
        ok = obtido == esperado
        falhas += not ok
        print(f"[{'OK' if ok else 'FALHOU'}] {descricao:<44} esperado={esperado} obtido={obtido}")

    # O dspy.GEPA (etapa 6) recusa métrica que não aceite estes cinco argumentos
    try:
        inspect.signature(bot.metrica_execucao).bind(None, None, None, None, None)
        print("[OK] Assinatura aceita pelo dspy.GEPA")
    except TypeError as e:
        falhas += 1
        print(f"[FALHOU] Assinatura aceita pelo dspy.GEPA: {e}")

    total = len(CASOS) + 1
    print(f"\n{total - falhas}/{total} casos passaram.")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
