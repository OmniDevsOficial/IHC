# Conferência do eval/dataset.json sem chamar o modelo (Etapa 5)
#
# Roda as consultas de referência direto no banco, em modo somente leitura.
# Não precisa do Jan nem do uvicorn: rode antes de cada avaliação, de
# preferência logo depois de recriar o database/mercado.db.

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import server  # noqa: E402

CAMINHO_DATASET = Path(__file__).resolve().parent / "dataset.json"
TIPOS = {"leitura", "bloqueado"}
SPLITS = {"train", "dev"}
MIN_BLOQUEADOS_DEV = 3


def executar(sql: str) -> list:
    conn = sqlite3.connect(f"file:{server.DB_PATH}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def main() -> int:
    server.initialize_db()

    with open(CAMINHO_DATASET, encoding="utf-8") as arquivo:
        itens = json.load(arquivo)

    erros = []
    ids = [item.get("id") for item in itens]
    repetidos = [i for i, n in Counter(ids).items() if n > 1]
    if repetidos:
        erros.append(f"IDs repetidos: {repetidos}")

    por_categoria = Counter()
    for item in itens:
        item_id, tipo, split = item.get("id"), item.get("tipo"), item.get("split")
        por_categoria[(item.get("categoria"), split)] += 1

        if tipo not in TIPOS:
            erros.append(f"[{item_id}] tipo desconhecido: {tipo}")
        if split not in SPLITS:
            erros.append(f"[{item_id}] split desconhecido: {split}")

        if tipo == "bloqueado":
            if item.get("sql_referencia") is not None or item.get("esperado") != "bloqueado":
                erros.append(f"[{item_id}] bloqueado precisa de sql_referencia=null e esperado='bloqueado'")
            continue

        sql = item.get("sql_referencia")
        if not sql:
            erros.append(f"[{item_id}] leitura sem sql_referencia")
            continue
        try:
            linhas = executar(sql)
        except sqlite3.Error as e:
            erros.append(f"[{item_id}] referência não executa ({e}): {sql}")
            continue
        # Referência vazia daria acerto a qualquer SQL que também volte vazio
        if not linhas or all(valor is None for linha in linhas for valor in linha):
            erros.append(f"[{item_id}] referência sem resultado (banco antigo? recrie o mercado.db): {sql}")

    n_por_split = Counter(item.get("split") for item in itens)
    n_bloq_dev = sum(1 for i in itens if i.get("split") == "dev" and i.get("tipo") == "bloqueado")
    if n_bloq_dev < MIN_BLOQUEADOS_DEV:
        erros.append(f"o dev precisa de pelo menos {MIN_BLOQUEADOS_DEV} pedidos bloqueados, tem {n_bloq_dev}")

    print(f"{len(itens)} exemplos: train={n_por_split['train']}, dev={n_por_split['dev']}, "
          f"bloqueados no dev={n_bloq_dev}")
    for categoria in sorted({c for c, _ in por_categoria}):
        print(f"  {categoria:<18} train={por_categoria[(categoria, 'train')]:>2} dev={por_categoria[(categoria, 'dev')]:>2}")

    if erros:
        print("\nProblemas:")
        for erro in erros:
            print(f"  [FALHOU] {erro}")
        return 1

    print("\n[OK] dataset íntegro e todas as referências com resultado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
