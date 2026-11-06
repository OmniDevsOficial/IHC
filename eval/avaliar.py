# Avaliação automática do projeto Text-to-SQL (Etapa 5)

import argparse
from collections import Counter
import json
import os
import re
import sqlite3
import sys
import time
import types
from pathlib import Path

# Garante que a raiz do projeto esteja no sys.path ao rodar `python eval/avaliar.py`
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

# Token falso antes de importar o bot.py (nunca lê nem imprime o token real)
os.environ.setdefault("TELEGRAM_API_TOKEN", "token-falso-so-para-teste")


def _garantir_stubs_se_sem_venv() -> None:
    """Cria stubs mínimos caso o script rode fora do venv (ex.: --seco ou teste_metrica)."""
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
        import fastapi  # noqa: F401
    except ModuleNotFoundError:
        mod_fastapi = types.ModuleType("fastapi")

        class _HTTPException(Exception):
            def __init__(self, status_code: int = 400, detail: str = ""):
                super().__init__(detail)
                self.status_code = status_code
                self.detail = detail

        class _FakeFastAPI:
            def __init__(self, *a, **kw):
                pass

            def get(self, *a, **kw):
                return lambda fn: fn

        mod_fastapi.FastAPI = _FakeFastAPI
        mod_fastapi.HTTPException = _HTTPException
        sys.modules["fastapi"] = mod_fastapi

    try:
        import dspy  # noqa: F401
    except ModuleNotFoundError:
        mod_dspy = types.ModuleType("dspy")

        class _FakeExample(dict):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.__dict__.update(kwargs)
                self._input_keys = set()

            def with_inputs(self, *keys):
                novo = _FakeExample(**dict(self))
                novo._input_keys = set(keys)
                return novo

            def inputs(self):
                return _FakeExample(**{k: self[k] for k in self._input_keys if k in self})

        class _FakePrediction(types.SimpleNamespace):
            pass

        class _FakeModule:
            def __call__(self, *args, **kwargs):
                return self.forward(*args, **kwargs)

        mod_dspy.LM = lambda *a, **kw: None
        mod_dspy.configure = lambda *a, **kw: None
        mod_dspy.Signature = type("Signature", (), {})
        mod_dspy.Module = _FakeModule
        mod_dspy.InputField = lambda *a, **kw: None
        mod_dspy.OutputField = lambda *a, **kw: None
        mod_dspy.ChainOfThought = lambda *a, **kw: None
        mod_dspy.Example = _FakeExample
        mod_dspy.Prediction = _FakePrediction
        sys.modules["dspy"] = mod_dspy


_garantir_stubs_se_sem_venv()

import dspy  # noqa: E402
import bot  # noqa: E402
import server  # noqa: E402

CAMINHO_DATASET = Path(__file__).resolve().parent / "dataset.json"
PASTA_RESULTADOS = Path(__file__).resolve().parent / "resultados"


def _conectar_somente_leitura() -> sqlite3.Connection:
    """Abre o banco real estritamente em modo somente leitura."""
    conn = sqlite3.connect(f"file:{server.DB_PATH}?mode=ro", uri=True)
    conn.set_authorizer(bot.bloquear_escrita)
    return conn


def _normalizar_linha(linha: tuple) -> tuple:
    return tuple(round(valor, 2) if isinstance(valor, float) else valor for valor in linha)


def _executar_sql_leitura(sql: str) -> list[tuple]:
    """Executa uma consulta de leitura no banco real (mode=ro) e devolve tuplas normalizadas."""
    if not sql or not isinstance(sql, str) or not sql.strip():
        raise sqlite3.ProgrammingError("SQL vazio")

    conn = _conectar_somente_leitura()
    try:
        cursor = conn.execute(sql)
        return [_normalizar_linha( tuple(linha) ) for linha in cursor.fetchall()]
    finally:
        conn.close()


def _tem_order_by(sql: str) -> bool:
    return bool(re.search(r"\bORDER\s+BY\b", sql or "", flags=re.IGNORECASE))


def carregar_dataset(caminho: Path = CAMINHO_DATASET) -> tuple[list, list]:
    """Carrega eval/dataset.json e devolve (trainset, devset) como dspy.Example."""
    server.initialize_db()
    dbschema = server.get_schema()
    ddl = server.get_ddl()

    with open(caminho, encoding="utf-8") as f:
        itens = json.load(f)

    trainset = []
    devset = []

    for item in itens:
        exemplo = dspy.Example(
            id=item["id"],
            dbschema=dbschema,
            question=item["pergunta"],
            ddl=ddl,
            sql_referencia=item.get("sql_referencia"),
            tipo=item["tipo"],
            categoria=item["categoria"],
            split=item["split"],
            esperado=item.get("esperado"),
        ).with_inputs("dbschema", "question", "ddl")

        if item["split"] == "train":
            trainset.append(exemplo)
        elif item["split"] == "dev":
            devset.append(exemplo)
        else:
            raise ValueError(f"Split inválido no exemplo {item['id']}: {item['split']}")

    return trainset, devset


def metrica_execucao(example, pred, trace=None) -> float:
    """Métrica compatível com dspy.Evaluate e GEPA (retorna 0.0 ou 1.0)."""
    tipo = getattr(example, "tipo", None) or example.get("tipo")
    erro_pred = getattr(pred, "erro", None)

    if tipo == "bloqueado":
        return 1.0 if erro_pred else 0.0

    if erro_pred:
        return 0.0

    sql_pred = getattr(pred, "sql_query", None)
    sql_ref = getattr(example, "sql_referencia", None) or example.get("sql_referencia")
    if not sql_pred or not sql_ref:
        return 0.0

    try:
        linhas_pred = _executar_sql_leitura(sql_pred)
        linhas_ref = _executar_sql_leitura(sql_ref)
    except Exception:
        return 0.0

    if _tem_order_by(sql_ref):
        return 1.0 if linhas_pred == linhas_ref else 0.0

    return 1.0 if Counter(linhas_pred) == Counter(linhas_ref) else 0.0


def verificar_dataset_seco(caminho: Path = CAMINHO_DATASET) -> int:
    """Valida integridade do dataset e executa todos os sql_referencia em modo somente leitura."""
    server.initialize_db()

    with open(caminho, encoding="utf-8") as f:
        itens = json.load(f)

    erros = []
    ids = [item.get("id") for item in itens]
    if len(ids) != len(set(ids)):
        erros.append("Existem IDs duplicados no dataset.")

    if len(itens) != 40:
        erros.append(f"Esperados 40 exemplos no total, encontrados {len(itens)}.")

    n_train = sum(1 for i in itens if i.get("split") == "train")
    n_dev = sum(1 for i in itens if i.get("split") == "dev")
    if n_train != 25 or n_dev != 15:
        erros.append(f" Divisão esperada 25 train / 15 dev, encontrada {n_train} train / {n_dev} dev.")

    n_leitura = sum(1 for i in itens if i.get("tipo") == "leitura")
    n_bloqueado = sum(1 for i in itens if i.get("tipo") == "bloqueado")
    if n_leitura != 30 or n_bloqueado != 10:
        erros.append(f"Esperados 30 leitura e 10 bloqueado, encontrados {n_leitura} e {n_bloqueado}.")

    n_bloq_dev = sum(1 for i in itens if i.get("split") == "dev" and i.get("tipo") == "bloqueado")
    if n_bloq_dev < 3:
        erros.append(f"O split dev precisa ter pelo menos 3 pedidos bloqueados, encontrados {n_bloq_dev}.")

    por_categoria = Counter()
    por_categoria_split = Counter()
    consultas_ok = 0

    for item in itens:
        item_id = item.get("id")
        tipo = item.get("tipo")
        cat = item.get("categoria")
        split = item.get("split")
        por_categoria[cat] += 1
        por_categoria_split[(cat, split)] += 1

        if tipo == "leitura":
            sql_ref = item.get("sql_referencia")
            if not sql_ref:
                erros.append(f"[{item_id}] Exemplo de leitura sem sql_referencia.")
                continue
            try:
                linhas = _executar_sql_leitura(sql_ref)
                if not linhas:
                    erros.append(f"[{item_id}] sql_referencia retornou 0 linhas: {sql_ref}")
                elif len(linhas) == 1 and all(v is None for v in linhas[0]):
                    erros.append(f"[{item_id}] sql_referencia retornou apenas NULL: {sql_ref}")
                else:
                    consultas_ok += 1
            except Exception as e:
                erros.append(f"[{item_id}] Erro ao executar sql_referencia ({e}): {sql_ref}")
        elif tipo == "bloqueado":
            if item.get("sql_referencia") is not None:
                erros.append(f"[{item_id}] Exemplo bloqueado deve ter sql_referencia=null.")
            if item.get("esperado") != "bloqueado":
                erros.append(f"[{item_id}] Exemplo bloqueado deve ter esperado='bloqueado'.")
        else:
            erros.append(f"[{item_id}] Tipo desconhecido: {tipo}")

    print("=== Verificação a seco do dataset (eval/dataset.json) ===")
    print(f"Total de exemplos : {len(itens)} (train={n_train}, dev={n_dev})")
    print(f"Por tipo          : leitura={n_leitura}, bloqueado={n_bloqueado} (dev bloqueados={n_bloq_dev})")
    print(f"SQLs de referência: {consultas_ok}/{n_leitura} executaram com >= 1 linha válida")
    print("\nDistribuição por categoria (total | train | dev):")
    for cat in sorted(por_categoria):
        t_cnt = por_categoria_split[(cat, "train")]
        d_cnt = por_categoria_split[(cat, "dev")]
        print(f"  - {cat:<18}: total={por_categoria[cat]:>2} | train={t_cnt:>2} | dev={d_cnt:>2}")

    if erros:
        print("\nERROS ENCONTRADOS:")
        for erro in erros:
            print(f"  [FALHOU] {erro}")
        return 1

    print("\n[OK] Dataset íntegro e todas as consultas de referência validadas em modo somente leitura.")
    return 0


def _calcular_resumo(registros: list[dict], nome_rodada: str, split: str, validar: bool) -> dict:
    leituras = [r for r in registros if r["tipo"] == "leitura"]
    bloqueados = [r for r in registros if r["tipo"] == "bloqueado"]

    exec_accuracy = (
        sum(r["acertou"] for r in leituras) / len(leituras) if leituras else 0.0
    )
    taxa_sql_valido = (
        sum(1 for r in leituras if r["sql_valido"]) / len(leituras) if leituras else 0.0
    )
    media_tentativas = (
        sum(r["tentativas"] for r in registros) / len(registros) if registros else 0.0
    )
    taxa_bloqueio = (
        sum(r["acertou"] for r in bloqueados) / len(bloqueados) if bloqueados else 0.0
    )
    latencia_media = (
        sum(r["latencia_s"] for r in registros) / len(registros) if registros else 0.0
    )

    categorias = sorted({r["categoria"] for r in registros})
    acerto_por_categoria = {}
    for cat in categorias:
        itens_cat = [r for r in registros if r["categoria"] == cat]
        acerto_por_categoria[cat] = {
            "total": len(itens_cat),
            "acertos": int(sum(r["acertou"] for r in itens_cat)),
            "taxa_acerto": round(sum(r["acertou"] for r in itens_cat) / len(itens_cat), 4),
        }

    return {
        "nome_rodada": nome_rodada,
        "split": split,
        "validacao_ativa": validar,
        "total_exemplos": len(registros),
        "total_leitura": len(leituras),
        "total_bloqueado": len(bloqueados),
        "execution_accuracy": round(exec_accuracy, 4),
        "taxa_sql_valido": round(taxa_sql_valido, 4),
        "media_tentativas": round(media_tentativas, 2),
        "taxa_bloqueio_correto": round(taxa_bloqueio, 4),
        "latencia_media_s": round(latencia_media, 2),
        "por_categoria": acerto_por_categoria,
    }


def rodar_avaliacao(args) -> int:
    trainset, devset = carregar_dataset()
    if args.split == "train":
        exemplos = trainset
    elif args.split == "dev":
        exemplos = devset
    else:
        exemplos = trainset + devset

    if args.limite is not None:
        exemplos = exemplos[: args.limite]

    validar = not args.sem_validacao
    nome_rodada = args.nome or ("cot-validacao" if validar else "cot-original")

    PASTA_RESULTADOS.mkdir(parents=True, exist_ok=True)
    caminho_jsonl = PASTA_RESULTADOS / f"{nome_rodada}.jsonl"
    caminho_resumo = PASTA_RESULTADOS / f"{nome_rodada}.resumo.json"

    registros_existentes = []
    ids_concluidos = set()
    if args.retomar and caminho_jsonl.exists():
        with open(caminho_jsonl, encoding="utf-8") as f:
            for linha in f:
                if linha.strip():
                    dado = json.loads(linha)
                    registros_existentes.append(dado)
                    ids_concluidos.add(dado["id"])
        print(f"Retomando '{nome_rodada}': {len(ids_concluidos)} exemplo(s) já gravado(s).")
    elif caminho_jsonl.exists():
        caminho_jsonl.unlink()

    registros = list(registros_existentes)

    for idx, ex in enumerate(exemplos, start=1):
        if ex.id in ids_concluidos:
            continue

        inicio = time.perf_counter()
        sql_gerado = None
        tentativas = 0
        bloqueado = False
        sql_valido = False
        acertou = 0.0
        status = "ok"
        detalhe_erro = None

        try:
            pred = bot.generator(
                dbschema=ex.dbschema,
                question=ex.question,
                ddl=ex.ddl,
                validar=validar,
            )
            latencia_s = time.perf_counter() - inicio
            tentativas = getattr(pred, "tentativas", 1)
            sql_gerado = getattr(pred, "sql_query", None)
            erro_pred = getattr(pred, "erro", None)
            bloqueado = bool(erro_pred)

            if bloqueado:
                status = "bloqueado"
                detalhe_erro = erro_pred
            elif sql_gerado:
                try:
                    _executar_sql_leitura(sql_gerado)
                    sql_valido = True
                except Exception as exc_sql:
                    sql_valido = False
                    detalhe_erro = str(exc_sql)

            acertou = metrica_execucao(ex, pred)
        except bot.ConsultaInvalida as exc_inv:
            latencia_s = time.perf_counter() - inicio
            tentativas = exc_inv.tentativas
            sql_valido = False
            acertou = 0.0
            status = "sql_invalido"
            detalhe_erro = str(exc_inv)
        except Exception as exc_mod:
            latencia_s = time.perf_counter() - inicio
            tentativas = 1
            sql_valido = False
            acertou = 0.0
            status = "erro_modelo"
            detalhe_erro = str(exc_mod)

        registro = {
            "id": ex.id,
            "split": ex.split,
            "tipo": ex.tipo,
            "categoria": ex.categoria,
            "pergunta": ex.question,
            "sql_referencia": ex.sql_referencia,
            "sql_gerado": sql_gerado,
            "tentativas": tentativas,
            "sql_valido": sql_valido,
            "bloqueado": bloqueado,
            "acertou": acertou,
            "latencia_s": round(latencia_s, 3),
            "status": status,
            "erro": detalhe_erro,
        }
        registros.append(registro)

        with open(caminho_jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")

        print(
            f"[{idx}/{len(exemplos)}] {ex.id} ({ex.categoria}) | status={status} "
            f"| acertou={acertou} | tentativas={tentativas} | {latencia_s:.1f}s"
        )

    resumo = _calcular_resumo(registros, nome_rodada, args.split, validar)
    with open(caminho_resumo, "w", encoding="utf-8") as f:
        json.dump(resumo, f, ensure_ascii=False, indent=2)

    print("\n=== Resumo da avaliação ===")
    print(f"Rodada                  : {resumo['nome_rodada']} (split={resumo['split']})")
    print(f"Exemplos avaliados      : {resumo['total_exemplos']} (leitura={resumo['total_leitura']}, bloqueado={resumo['total_bloqueado']})")
    print(f"Execution accuracy      : {resumo['execution_accuracy'] * 100:.1f}%")
    print(f"Taxa de SQL válido      : {resumo['taxa_sql_valido'] * 100:.1f}%")
    print(f"Tentativas (média)      : {resumo['media_tentativas']:.2f}")
    print(f"Taxa de bloqueio correto: {resumo['taxa_bloqueio_correto'] * 100:.1f}%")
    print(f"Latência média          : {resumo['latencia_media_s']:.2f}s")
    print(f"Arquivos gerados        : {caminho_jsonl} e {caminho_resumo}")
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description="Avaliador Text-to-SQL (Etapa 5)")
    parser.add_argument(
        "--split",
        choices=["train", "dev", "todos"],
        default="dev",
        help="Conjunto de exemplos para avaliar (padrão: dev)",
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=None,
        help="Avalia apenas os N primeiros exemplos do split escolhido",
    )
    parser.add_argument(
        "--sem-validacao",
        action="store_true",
        help="Desliga limpar_sql e validar_sql para medir o CoT original",
    )
    parser.add_argument(
        "--nome",
        type=str,
        default=None,
        help="Nome da rodada para os arquivos em eval/resultados/",
    )
    parser.add_argument(
        "--retomar",
        action="store_true",
        help="Pula exemplos cujo id já esteja gravado no .jsonl da rodada",
    )
    parser.add_argument(
        "--seco",
        action="store_true",
        help="Apenas valida o dataset e executa os sql_referencia sem chamar o modelo",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.seco:
        return verificar_dataset_seco()
    return rodar_avaliacao(args)


if __name__ == "__main__":
    sys.exit(main())
