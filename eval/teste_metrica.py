# Teste unitário da métrica de execução sem chamar o modelo (Etapa 5)

import os
import sys
from pathlib import Path

# Garante que a raiz e a pasta eval estejam no sys.path
PASTA_EVAL = Path(__file__).resolve().parent
RAIZ = PASTA_EVAL.parent
for caminho in (str(RAIZ), str(PASTA_EVAL)):
    if caminho not in sys.path:
        sys.path.insert(0, caminho)

os.environ.setdefault("TELEGRAM_API_TOKEN", "token-falso-so-para-teste")

from avaliar import dspy, metrica_execucao  # noqa: E402
import server  # noqa: E402


def main() -> int:
    server.initialize_db()

    ex_leitura = dspy.Example(
        id="t1",
        tipo="leitura",
        categoria="join_categoria",
        sql_referencia="SELECT nome FROM categorias",
    )

    ex_order_by = dspy.Example(
        id="t2",
        tipo="leitura",
        categoria="preco",
        sql_referencia="SELECT nome, preco_venda FROM produtos ORDER BY preco_venda DESC LIMIT 5",
    )

    ex_bloqueado = dspy.Example(
        id="t3",
        tipo="bloqueado",
        categoria="bloqueado",
        sql_referencia=None,
    )

    casos = [
        (
            "Mesmo resultado com outra ordem de linhas e outro alias",
            ex_leitura,
            dspy.Prediction(
                sql_query="SELECT nome AS departamento FROM categorias ORDER BY nome DESC",
                erro=None,
            ),
            1.0,
        ),
        (
            "Resultado diferente do esperado",
            ex_leitura,
            dspy.Prediction(
                sql_query="SELECT nome FROM categorias WHERE nome = 'Bebidas'",
                erro=None,
            ),
            0.0,
        ),
        (
            "SQL com erro de sintaxe ou coluna inexistente",
            ex_leitura,
            dspy.Prediction(
                sql_query="SELECT coluna_que_nao_existe FROM categorias",
                erro=None,
            ),
            0.0,
        ),
        (
            "Pedido bloqueado com erro preenchido",
            ex_bloqueado,
            dspy.Prediction(
                sql_query=None,
                erro="Bloqueado: Apenas consultas (SELECT) são permitidas.",
            ),
            1.0,
        ),
        (
            "Pedido bloqueado sem erro preenchido",
            ex_bloqueado,
            dspy.Prediction(
                sql_query="SELECT 1",
                erro=None,
            ),
            0.0,
        ),
        (
            "Referência com ORDER BY em ordem trocada",
            ex_order_by,
            dspy.Prediction(
                sql_query=(
                    "SELECT * FROM ("
                    "SELECT nome, preco_venda FROM produtos ORDER BY preco_venda DESC LIMIT 5"
                    ") ORDER BY preco_venda ASC"
                ),
                erro=None,
            ),
            0.0,
        ),
    ]

    falhas = 0
    for descricao, exemplo, pred, esperado in casos:
        obtido = metrica_execucao(exemplo, pred)
        status = "OK" if obtido == esperado else "FALHOU"
        if status == "FALHOU":
            falhas += 1
        print(f"[{status}] {descricao:<56} | esperado={esperado} obtido={obtido}")

    print(f"\nResumo: {len(casos) - falhas}/{len(casos)} casos passaram.")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
