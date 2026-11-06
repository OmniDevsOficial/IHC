# Teste da rota /query e camada somente leitura do servidor (Etapa 4.2)

import sys
import types
from contextlib import contextmanager
from pathlib import Path

# Garante que a raiz do projeto esteja no sys.path ao rodar `python eval/teste_servidor.py`
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))


def _obter_test_client_e_app():
    """Usa fastapi.testclient.TestClient quando disponível; caso contrário usa shim compatível."""
    try:
        from fastapi.testclient import TestClient
        from server import app
        return TestClient, app, True
    except (ModuleNotFoundError, RuntimeError) as exc:
        print(f"Aviso: TestClient oficial indisponível no ambiente atual ({exc}); usando cliente local de fallback.")

        class _HTTPException(Exception):
            def __init__(self, status_code: int, detail: str):
                super().__init__(detail)
                self.status_code = status_code
                self.detail = detail

        class _FakeFastAPI:
            def __init__(self, title: str = "", lifespan=None):
                self.title = title
                self.lifespan = lifespan
                self.routes = {}

            def get(self, path: str):
                def decorator(fn):
                    self.routes[path] = fn
                    return fn
                return decorator

        if "fastapi" not in sys.modules:
            mod_fastapi = types.ModuleType("fastapi")
            mod_fastapi.FastAPI = _FakeFastAPI
            mod_fastapi.HTTPException = _HTTPException
            sys.modules["fastapi"] = mod_fastapi

        import server

        class _RespostaLocal:
            def __init__(self, status_code: int, payload: dict):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        @contextmanager
        def _FallbackTestClient(app_instance):
            server.initialize_db()

            class _Client:
                def get(self, path: str, params: dict | None = None):
                    params = params or {}
                    fn = app_instance.routes[path]
                    try:
                        corpo = fn(**params)
                        return _RespostaLocal(200, corpo)
                    except server.HTTPException as e:
                        return _RespostaLocal(e.status_code, {"detail": e.detail})

            yield _Client()

        return _FallbackTestClient, server.app, False


TestClient, app, USANDO_TESTCLIENT_OFICIAL = _obter_test_client_e_app()

CASOS = [
    (
        "GET /query?sql=SELECT nome FROM categorias",
        {"sql": "SELECT nome FROM categorias"},
        200,
    ),
    (
        "GET /query?sql=WITH t AS (SELECT 1 AS a) SELECT a FROM t",
        {"sql": "WITH t AS (SELECT 1 AS a) SELECT a FROM t"},
        200,
    ),
    (
        "GET /query?sql=DELETE FROM produtos",
        {"sql": "DELETE FROM produtos"},
        400,
    ),
    (
        "GET /query?sql=WITH t AS (SELECT 1) DELETE FROM produtos",
        {"sql": "WITH t AS (SELECT 1) DELETE FROM produtos"},
        400,
    ),
    (
        "GET /query (sem parâmetros)",
        {},
        400,
    ),
]


def main() -> int:
    falhas = 0
    with TestClient(app) as client:
        for descricao, params, status_esperado in CASOS:
            resposta = client.get("/query", params=params)
            obtido = resposta.status_code
            status = "OK" if obtido == status_esperado else "FALHOU"
            if status == "FALHOU":
                falhas += 1
            print(
                f"[{status}] {descricao:<56} | esperado={status_esperado} obtido={obtido}"
            )

    print(f"\nResumo: {len(CASOS) - falhas}/{len(CASOS)} casos passaram.")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
