# Backend do projeto IHC (Text-to-SQL via Telegram)

import sqlite3
import os
import random
from datetime import date, timedelta
from typing import Optional
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
    nome TEXT              -- departamentos
)

fornecedores(
    id INTEGER PRIMARY KEY,
    nome TEXT
)

produtos(
    id INTEGER PRIMARY KEY,
    nome TEXT,
    marca TEXT,
    unidade_medida TEXT,   -- 'un', 'kg' ou 'L'
    codigo_barras TEXT,
    preco_custo REAL,
    preco_venda REAL,
    estoque_minimo INTEGER,
    categoria_id INTEGER,  -- referencia categorias.id
    fornecedor_id INTEGER  -- referencia fornecedores.id
)

lotes(
    id INTEGER PRIMARY KEY,
    produto_id INTEGER,    -- referencia produtos.id
    quantidade INTEGER,    -- quantidade atual do lote
    data_fabricacao TEXT,  -- YYYY-MM-DD
    data_validade TEXT,    -- YYYY-MM-DD
    data_entrada TEXT,     -- YYYY-MM-DD
    localizacao TEXT
)

movimentacoes(
    id INTEGER PRIMARY KEY,
    produto_id INTEGER,    -- referencia produtos.id
    lote_id INTEGER,       -- referencia lotes.id
    tipo TEXT,             -- 'entrada' ou 'saida'
    quantidade INTEGER,
    data TEXT              -- YYYY-MM-DD
)

Regras:
- Estoque total de um produto = SUM(lotes.quantidade) daquele produto.
- Para a categoria de um produto, use JOIN entre produtos.categoria_id e categorias.id.
- Datas estão em texto YYYY-MM-DD; use date('now') para a data de hoje.
- Vendas são movimentacoes com tipo = 'saida'.
"""

def get_schema() -> str:
    return SCHEMA_DESCRICAO

def get_ddl() -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    try:
        linhas = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return [linha[0] for linha in linhas]
    finally:
        conn.close()

def _criar_tabelas(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS categorias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE COLLATE NOCASE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fornecedores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE COLLATE NOCASE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS produtos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL COLLATE NOCASE,
            marca TEXT,
            unidade_medida TEXT NOT NULL,
            codigo_barras TEXT,
            preco_custo REAL NOT NULL,
            preco_venda REAL NOT NULL,
            estoque_minimo INTEGER NOT NULL DEFAULT 0,
            categoria_id INTEGER NOT NULL,
            fornecedor_id INTEGER,
            FOREIGN KEY (categoria_id) REFERENCES categorias(id),
            FOREIGN KEY (fornecedor_id) REFERENCES fornecedores(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            produto_id INTEGER NOT NULL,
            quantidade INTEGER NOT NULL,
            data_fabricacao TEXT,
            data_validade TEXT,
            data_entrada TEXT NOT NULL,
            localizacao TEXT,
            FOREIGN KEY (produto_id) REFERENCES produtos(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS movimentacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            produto_id INTEGER NOT NULL,
            lote_id INTEGER,
            tipo TEXT NOT NULL,
            quantidade INTEGER NOT NULL,
            data TEXT NOT NULL,
            FOREIGN KEY (produto_id) REFERENCES produtos(id),
            FOREIGN KEY (lote_id) REFERENCES lotes(id)
        )
    """)


def _popular_dados_exemplo(conn: sqlite3.Connection) -> None:
    # Seed fixa: o banco sai igual toda vez que é recriado.
    # As datas são relativas a hoje, então sempre existem lotes
    # vencidos, lotes perto de vencer e vendas recentes.
    rng = random.Random(42)
    hoje = date.today()

    def dia(dias_atras: int) -> str:
        return (hoje - timedelta(days=dias_atras)).isoformat()

    # Categorias (departamentos)
    categorias = ["Higiene", "Bebidas", "Alimentos", "Limpeza", "Hortifruti"]
    conn.executemany(
        "INSERT INTO categorias (nome) VALUES (?)",
        [(c,) for c in categorias],
    )
    cat_id = dict(conn.execute("SELECT nome, id FROM categorias").fetchall())

    # Fornecedores
    fornecedor_da_categoria = {
        "Higiene": "Distribuidora Sul",
        "Limpeza": "Distribuidora Sul",
        "Bebidas": "Atacado Central",
        "Alimentos": "Atacado Central",
        "Hortifruti": "Hortifruti do Vale",
    }
    conn.executemany(
        "INSERT INTO fornecedores (nome) VALUES (?)",
        [(f,) for f in sorted(set(fornecedor_da_categoria.values()))],
    )
    forn_id = dict(conn.execute("SELECT nome, id FROM fornecedores").fetchall())

    corredor = {"Higiene": 1, "Bebidas": 2, "Alimentos": 3, "Limpeza": 4, "Hortifruti": 5}

    # Validade (em dias) de cada produto
    validade_padrao = {
        "Higiene": 720, "Bebidas": 300, "Alimentos": 365,
        "Limpeza": 720, "Hortifruti": 10,
    }
    validade_produto = {
        "Água mineral 500ml": 720, "Leite 1L": 60, "Pão de forma": 10,
        "Ovos (dúzia)": 35, "Suco de laranja 1L": 120,
        "Suco de uva 1L": 120, "Alface (unidade)": 7,
    }
    # Dias até vencer o lote mais antigo (negativo = já vencido)
    vence_em = {"Pão de forma": -2, "Leite 1L": 4, "Suco de uva 1L": 6, "Ovos (dúzia)": 3}

    # (nome, marca, unidade, preco_custo, preco_venda, estoque_minimo, qtd_atual, categoria)
    produtos = [
        # Higiene
        ("Sabonete", "Dove", "un", 2.40, 3.50, 30, 120, "Higiene"),
        ("Shampoo", "Seda", "un", 11.00, 15.90, 15, 8, "Higiene"),
        ("Condicionador", "Seda", "un", 11.80, 16.90, 15, 35, "Higiene"),
        ("Pasta de dente", "Colgate", "un", 3.50, 5.20, 25, 90, "Higiene"),
        ("Escova de dente", "Oral-B", "un", 2.60, 4.00, 25, 100, "Higiene"),
        ("Papel higiênico (4 rolos)", "Neve", "un", 6.50, 9.90, 40, 150, "Higiene"),
        ("Desodorante", "Rexona", "un", 7.90, 11.50, 20, 75, "Higiene"),

        # Bebidas
        ("Água mineral 500ml", "Crystal", "un", 1.10, 2.00, 100, 300, "Bebidas"),
        ("Coca-Cola 2L", "Coca-Cola", "L", 6.80, 9.99, 30, 80, "Bebidas"),
        ("Guaraná 2L", "Antarctica", "L", 6.10, 8.99, 30, 70, "Bebidas"),
        ("Suco de laranja 1L", "Del Valle", "L", 5.30, 7.50, 20, 60, "Bebidas"),
        ("Suco de uva 1L", "Aurora", "L", 5.50, 7.80, 20, 55, "Bebidas"),
        ("Cerveja lata 350ml", "Skol", "un", 2.60, 3.99, 60, 45, "Bebidas"),
        ("Café 500g", "Pilão", "un", 10.50, 14.90, 20, 65, "Bebidas"),
        ("Leite 1L", "Italac", "L", 3.90, 5.49, 50, 30, "Bebidas"),

        # Alimentos
        ("Arroz 5kg", "Tio João", "un", 18.00, 24.90, 15, 10, "Alimentos"),
        ("Feijão 1kg", "Camil", "un", 5.90, 8.30, 25, 70, "Alimentos"),
        ("Macarrão 500g", "Barilla", "un", 3.10, 4.50, 30, 110, "Alimentos"),
        ("Açúcar 1kg", "União", "un", 3.50, 4.99, 30, 95, "Alimentos"),
        ("Óleo de soja 900ml", "Liza", "un", 5.20, 7.20, 25, 85, "Alimentos"),
        ("Farinha de trigo 1kg", "Dona Benta", "un", 4.00, 5.80, 20, 60, "Alimentos"),
        ("Molho de tomate 340g", "Fugini", "un", 2.20, 3.30, 40, 130, "Alimentos"),
        ("Biscoito recheado", "Bauducco", "un", 2.60, 3.99, 50, 160, "Alimentos"),
        ("Pão de forma", "Wickbold", "un", 4.60, 6.50, 30, 90, "Alimentos"),
        ("Ovos (dúzia)", "Mantiqueira", "un", 7.00, 9.90, 30, 100, "Alimentos"),

        # Limpeza
        ("Detergente", "Ypê", "un", 1.70, 2.50, 50, 150, "Limpeza"),
        ("Sabão em pó 1kg", "Omo", "un", 8.50, 12.00, 15, 45, "Limpeza"),
        ("Água sanitária 1L", "Qboa", "L", 3.30, 4.90, 30, 100, "Limpeza"),
        ("Desinfetante 500ml", "Pinho Sol", "un", 4.40, 6.30, 30, 90, "Limpeza"),
        ("Esponja de aço", "Bombril", "un", 1.50, 2.20, 60, 200, "Limpeza"),
        ("Amaciante 2L", "Comfort", "L", 9.80, 13.90, 15, 55, "Limpeza"),

        # Hortifruti (vendidos por kg ou unidade)
        ("Banana (kg)", None, "kg", 3.20, 4.99, 50, 200, "Hortifruti"),
        ("Maçã (kg)", None, "kg", 4.50, 6.49, 40, 180, "Hortifruti"),
        ("Tomate (kg)", None, "kg", 5.60, 7.99, 30, 12, "Hortifruti"),
        ("Cebola (kg)", None, "kg", 3.70, 5.50, 30, 110, "Hortifruti"),
        ("Batata (kg)", None, "kg", 2.90, 4.20, 40, 130, "Hortifruti"),
        ("Alface (unidade)", None, "un", 1.90, 2.99, 20, 70, "Hortifruti"),
        ("Laranja (kg)", None, "kg", 2.70, 3.99, 40, 150, "Hortifruti"),
    ]

    for i, (nome, marca, unidade, custo, venda, minimo, qtd, categoria) in enumerate(produtos, start=1):
        produto_id = conn.execute(
            """INSERT INTO produtos
               (nome, marca, unidade_medida, codigo_barras, preco_custo,
                preco_venda, estoque_minimo, categoria_id, fornecedor_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (nome, marca, unidade, f"7890000{i:06d}", custo, venda, minimo,
             cat_id[categoria], forn_id[fornecedor_da_categoria[categoria]]),
        ).lastrowid

        validade = validade_produto.get(nome, validade_padrao[categoria])
        local = f"Corredor {corredor[categoria]}, Prateleira {rng.choice('ABCD')}"
        qtd_antigo = round(qtd * 0.4)
        qtd_novo = qtd - qtd_antigo

        # Lote antigo
        if nome in vence_em:
            validade_antigo = hoje + timedelta(days=vence_em[nome])
            fab_antigo = validade_antigo - timedelta(days=validade)
            ent_antigo = fab_antigo + timedelta(days=1)
        else:
            idade = max(2, min(int(validade * rng.uniform(0.2, 0.8)), rng.randint(40, 120)))
            fab_antigo = hoje - timedelta(days=idade)
            ent_antigo = min(fab_antigo + timedelta(days=rng.randint(0, 3)), hoje)
            validade_antigo = fab_antigo + timedelta(days=validade)

        # Lote novo (sempre mais recente que o antigo)
        dias_antigo = (hoje - fab_antigo).days
        idade_novo = rng.randint(1, max(1, min(25, validade // 2, dias_antigo - 1)))
        fab_novo = hoje - timedelta(days=idade_novo)
        ent_novo = min(fab_novo + timedelta(days=rng.randint(0, 1)), hoje)
        validade_novo = fab_novo + timedelta(days=validade)

        # Vendas dos últimos 30 dias (saem do lote antigo)
        dias_max = (hoje - ent_antigo).days
        vendas = []
        for k in range(rng.randint(8, 14)):
            teto = min(6, dias_max) if k == 0 else min(29, dias_max)
            vendas.append((rng.randint(0, teto), rng.randint(1, max(2, minimo // 4))))
        vendido = sum(q for _, q in vendas)

        lote_antigo = conn.execute(
            """INSERT INTO lotes (produto_id, quantidade, data_fabricacao,
               data_validade, data_entrada, localizacao)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (produto_id, qtd_antigo, fab_antigo.isoformat(),
             validade_antigo.isoformat(), ent_antigo.isoformat(), local),
        ).lastrowid
        lote_novo = conn.execute(
            """INSERT INTO lotes (produto_id, quantidade, data_fabricacao,
               data_validade, data_entrada, localizacao)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (produto_id, qtd_novo, fab_novo.isoformat(),
             validade_novo.isoformat(), ent_novo.isoformat(), local),
        ).lastrowid

        # Movimentações: uma entrada por lote + as saídas (vendas)
        conn.execute(
            "INSERT INTO movimentacoes (produto_id, lote_id, tipo, quantidade, data) VALUES (?, ?, 'entrada', ?, ?)",
            (produto_id, lote_antigo, qtd_antigo + vendido, ent_antigo.isoformat()),
        )
        conn.execute(
            "INSERT INTO movimentacoes (produto_id, lote_id, tipo, quantidade, data) VALUES (?, ?, 'entrada', ?, ?)",
            (produto_id, lote_novo, qtd_novo, ent_novo.isoformat()),
        )
        conn.executemany(
            "INSERT INTO movimentacoes (produto_id, lote_id, tipo, quantidade, data) VALUES (?, ?, 'saida', ?, ?)",
            [(produto_id, lote_antigo, q, dia(d)) for d, q in vendas],
        )

    conn.commit()


def initialize_db() -> None:
    os.makedirs(DB_DIR, exist_ok=True)
    banco_novo = not os.path.exists(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
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
def query(sql: Optional[str] = None, schema: bool = False):
    # Com schema=true, ignora o sql e devolve a descrição e o DDL das tabelas
    if schema:
        return {"descricao": get_schema(), "ddl": get_ddl()}

    if not sql:
        raise HTTPException(status_code=400, detail="Informe o parâmetro 'sql' ou use schema=true.")

    try:
        resultado = execute_query(sql)
        return {"sql": sql, "resultado": resultado}
    except sqlite3.Error as e:
        raise HTTPException(status_code=400, detail=f"Erro ao executar SQL: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))