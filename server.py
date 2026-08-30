# Important: We need to implement the FastAPI routing, so that bot.py only connects with this server.py by HTTP (GET) Requests

import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(BASE_DIR, "database/lojas.db")

schema_db = """
CREATE TABLE produtos (
    nome TEXT,
    departmento TEXT,
);
"""

def create_db():
  conn = sqlite3.connect(db_path)
  c = conn.cursor()

  # Create tables
  c.execute("""CREATE TABLE IF NOT EXISTS produtos (
                nome TEXT, 
                departamento TEXT
            )""")

  c.executemany("INSERT INTO produtos VALUES (?, ?)", [
    ("sabonete", "higiene"),
    ("agua", "bebidas"),
    ("coca", "bebidas"),
  ])

  conn.commit()
  conn.close()

# DB Connection Test: print all products on the terminal
def query_test():
    conn = sqlite3.connect(db_path)
    results = conn.execute("SELECT * from produtos").fetchall()
    print(results)

def initialize_db():
    # Create DB only if needed
    if os.path.exists(db_path) != True:
        os.mkdir("database")
        create_db()
    # After creation/checking, test the DB
    query_test()

def execute_query(sql):
    conn = sqlite3.connect(db_path)
    query_results = conn.execute(sql).fetchall()
    return query_results

def get_schema():
    return schema_db