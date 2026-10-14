#!/usr/bin/env python3
"""
Convertit une base Postgres (Synapse) en base SQLite équivalente.
Introspecte le schéma via information_schema, crée les tables en SQLite,
puis copie les données table par table.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import psycopg2
import psycopg2.extras

PG_DSN = os.environ["PG_DSN"]
SQLITE_PATH = "synapse_sqlite.db"

# Mapping simplifié des types Postgres -> types SQLite (SQLite est faiblement
# typé, donc on peut se permettre d'être assez permissif ici).
TYPE_MAP = {
    "integer": "INTEGER",
    "bigint": "INTEGER",
    "smallint": "INTEGER",
    "boolean": "INTEGER",
    "text": "TEXT",
    "character varying": "TEXT",
    "varchar": "TEXT",
    "bytea": "BLOB",
    "double precision": "REAL",
    "real": "REAL",
    "numeric": "REAL",
    "timestamp without time zone": "TEXT",
    "timestamp with time zone": "TEXT",
    "jsonb": "TEXT",
    "json": "TEXT",
    "uuid": "TEXT",
    "ARRAY": "TEXT",  # les tableaux Postgres sont sérialisés en texte, pas idéal mais fonctionnel
}


def pg_type_to_sqlite(pg_type: str) -> str:
    return TYPE_MAP.get(pg_type, "TEXT")


def get_tables(pg_cur) -> list[str]:
    pg_cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name;
        """
    )
    return [row[0] for row in pg_cur.fetchall()]


def get_columns(pg_cur, table: str) -> list[tuple[str, str]]:
    pg_cur.execute(
        """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position;
        """,
        (table,),
    )
    return [(row[0], row[1]) for row in pg_cur.fetchall()]


def get_primary_key(pg_cur, table: str) -> list[str]:
    pg_cur.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary;
        """,
        (table,),
    )
    return [row[0] for row in pg_cur.fetchall()]


def create_sqlite_table(sqlite_cur, table: str, columns: list[tuple[str, str]], pk: list[str]) -> None:
    col_defs = []
    for name, pg_type in columns:
        sqlite_type = pg_type_to_sqlite(pg_type)
        col_defs.append(f'"{name}" {sqlite_type}')

    pk_clause = ""
    if pk:
        pk_cols = ", ".join(f'"{c}"' for c in pk)
        pk_clause = f", PRIMARY KEY ({pk_cols})"

    ddl = f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(col_defs)}{pk_clause});'
    sqlite_cur.execute(ddl)


def main() -> None:
    print(f"Connexion à Postgres...")
    pg_conn = psycopg2.connect(PG_DSN)
    pg_meta_cur = pg_conn.cursor()  # curseur normal pour les requêtes de métadonnées

    print(f"Création de la base SQLite: {SQLITE_PATH}")
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_cur = sqlite_conn.cursor()
    sqlite_cur.execute("PRAGMA journal_mode=WAL;")

    tables = get_tables(pg_meta_cur)
    print(f"{len(tables)} tables trouvées.")

    for i, table in enumerate(tables, 1):
        print(f"[{i}/{len(tables)}] Table: {table}")
        columns = get_columns(pg_meta_cur, table)
        pk = get_primary_key(pg_meta_cur, table)

        create_sqlite_table(sqlite_cur, table, columns, pk)
        sqlite_conn.commit()

        col_names = [c[0] for c in columns]
        col_list = ", ".join(f'"{c}"' for c in col_names)

        # Nouveau curseur nommé (server-side) à chaque table: un curseur nommé
        # Postgres ne supporte qu'un seul execute() avant d'être fermé.
        pg_cur = pg_conn.cursor(name=f"cur_{i}")
        pg_cur.itersize = 1000
        pg_cur.execute(f'SELECT {col_list} FROM "{table}";')

        placeholders = ", ".join("?" for _ in col_names)
        insert_sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders});'

        row_count = 0
        batch = []
        batch_size = 1000

        for row in pg_cur:
            converted = []
            for val in row:
                if isinstance(val, (dict, list)):
                    import json
                    converted.append(json.dumps(val))
                elif isinstance(val, memoryview):
                    converted.append(bytes(val))
                elif isinstance(val, bool):
                    converted.append(int(val))
                else:
                    converted.append(val)
            batch.append(tuple(converted))
            row_count += 1

            if len(batch) >= batch_size:
                sqlite_cur.executemany(insert_sql, batch)
                batch = []

        if batch:
            sqlite_cur.executemany(insert_sql, batch)

        sqlite_conn.commit()
        pg_cur.close()
        print(f"    -> {row_count} lignes copiées.")

    pg_meta_cur.close()
    pg_conn.close()
    sqlite_conn.close()
    print("Conversion terminée.")


if __name__ == "__main__":
    main()
