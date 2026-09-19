# Backend do projeto IHC (Text-to-SQL via Telegram)

import sqlite3
import os
from fastapi import FastAPI, HTTPException

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "database")
DB_PATH = os.path.join(DB_DIR, "mercado.db")

app = FastAPI(title="Mercado DB API")

# Schema

SCHEMA_DESCRICAO = """
Tabelas disponíveis:

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

Para perguntas envolvendo categoria de um produto, use JOIN entre
produtos.categoria_id e categorias.id.
"""

def get_schema() -> str:
    return SCHEMA_DESCRICAO

def _criar_tabelas(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS categorias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE COLLATE NOCASE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS produtos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL COLLATE NOCASE,
            preco REAL NOT NULL,
            estoque INTEGER NOT NULL DEFAULT 0,
            categoria_id INTEGER NOT NULL,
            FOREIGN KEY (categoria_id) REFERENCES categorias(id)
        )
    """)


def _popular_dados_exemplo(conn: sqlite3.Connection) -> None:
    categorias = ["Higiene", "Bebidas", "Alimentos", "Limpeza", "Hortifruti"]
    conn.executemany(
        "INSERT INTO categorias (nome) VALUES (?)",
        [(c,) for c in categorias],
    )

    ids = dict(conn.execute("SELECT nome, id FROM categorias").fetchall())

    produtos = [
        # Higiene
        ("Sabonete", 3.50, 120, ids["Higiene"]),
        ("Shampoo", 15.90, 40, ids["Higiene"]),
        ("Condicionador", 16.90, 35, ids["Higiene"]),
        ("Pasta de dente", 5.20, 90, ids["Higiene"]),
        ("Escova de dente", 4.00, 100, ids["Higiene"]),
        ("Papel higiênico (4 rolos)", 9.90, 150, ids["Higiene"]),
        ("Desodorante", 11.50, 75, ids["Higiene"]),

        # Bebidas
        ("Água mineral 500ml", 2.00, 300, ids["Bebidas"]),
        ("Coca-Cola 2L", 9.99, 80, ids["Bebidas"]),
        ("Guaraná 2L", 8.99, 70, ids["Bebidas"]),
        ("Suco de laranja 1L", 7.50, 60, ids["Bebidas"]),
        ("Suco de uva 1L", 7.80, 55, ids["Bebidas"]),
        ("Cerveja lata 350ml", 3.99, 200, ids["Bebidas"]),
        ("Café 500g", 14.90, 65, ids["Bebidas"]),
        ("Leite 1L", 5.49, 140, ids["Bebidas"]),

        # Alimentos
        ("Arroz 5kg", 24.90, 50, ids["Alimentos"]),
        ("Feijão 1kg", 8.30, 70, ids["Alimentos"]),
        ("Macarrão 500g", 4.50, 110, ids["Alimentos"]),
        ("Açúcar 1kg", 4.99, 95, ids["Alimentos"]),
        ("Óleo de soja 900ml", 7.20, 85, ids["Alimentos"]),
        ("Farinha de trigo 1kg", 5.80, 60, ids["Alimentos"]),
        ("Molho de tomate 340g", 3.30, 130, ids["Alimentos"]),
        ("Biscoito recheado", 3.99, 160, ids["Alimentos"]),
        ("Pão de forma", 6.50, 90, ids["Alimentos"]),
        ("Ovos (dúzia)", 9.90, 100, ids["Alimentos"]),

        # Limpeza
        ("Detergente", 2.50, 150, ids["Limpeza"]),
        ("Sabão em pó 1kg", 12.00, 45, ids["Limpeza"]),
        ("Água sanitária 1L", 4.90, 100, ids["Limpeza"]),
        ("Desinfetante 500ml", 6.30, 90, ids["Limpeza"]),
        ("Esponja de aço", 2.20, 200, ids["Limpeza"]),
        ("Amaciante 2L", 13.90, 55, ids["Limpeza"]),

        # Hortifruti
        ("Banana (kg)", 4.99, 200, ids["Hortifruti"]),
        ("Maçã (kg)", 6.49, 180, ids["Hortifruti"]),
        ("Tomate (kg)", 7.99, 90, ids["Hortifruti"]),
        ("Cebola (kg)", 5.50, 110, ids["Hortifruti"]),
        ("Batata (kg)", 4.20, 130, ids["Hortifruti"]),
        ("Alface (unidade)", 2.99, 70, ids["Hortifruti"]),
        ("Laranja (kg)", 3.99, 150, ids["Hortifruti"]),
    ]
    conn.executemany(
        "INSERT INTO produtos (nome, preco, estoque, categoria_id) VALUES (?, ?, ?, ?)",
        produtos,
    )
    conn.commit()


def initialize_db() -> None:
    os.makedirs(DB_DIR, exist_ok=True)
    banco_novo = not os.path.exists(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    try:
        _criar_tabelas(conn)
        if banco_novo:
            _popular_dados_exemplo(conn)
        conn.commit()
    finally:
        conn.close()


# Execução de queries geradas pela IA

def execute_query(sql: str):
    sql_normalizado = sql.strip().lower()
    if not sql_normalizado.startswith("select"):
        raise ValueError("Por segurança, só são permitidas consultas SELECT.")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(sql)
        linhas = cursor.fetchall()
        return [dict(linha) for linha in linhas]
    finally:
        conn.close()

# API 

@app.on_event("startup")
def _on_startup():
    initialize_db()

@app.get("/query")
def query(sql: str):
 
    try:
        resultado = execute_query(sql)
        return {"sql": sql, "resultado": resultado}
    except sqlite3.Error as e:
        raise HTTPException(status_code=400, detail=f"Erro ao executar SQL: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))