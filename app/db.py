from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.deps import KINSHIP_EDITOR_EMAIL
from app.models import Base


def sqlite_url(path: Path) -> str:
    return "sqlite:///" + path.resolve().as_posix()


def make_engine(database_path: Path) -> Engine:
    engine = create_engine(sqlite_url(database_path), connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def enable_sqlite(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def make_session_factory(engine: Engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_tables(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        found = conn.exec_driver_sql("PRAGMA table_info(link_rows)").fetchall()
        if found and not any(row[1] == "person_id" for row in found):
            conn.exec_driver_sql("ALTER TABLE link_rows ADD COLUMN person_id INTEGER")
        datasets = conn.exec_driver_sql("PRAGMA table_info(datasets)").fetchall()
        if datasets and not any(row[1] == "owner_name" for row in datasets):
            conn.exec_driver_sql("ALTER TABLE datasets ADD COLUMN owner_name VARCHAR(255) NOT NULL DEFAULT ''")
        spaces = conn.exec_driver_sql("PRAGMA table_info(spaces)").fetchall()
        if spaces and not any(row[1] == "cluster_self" for row in spaces):
            conn.exec_driver_sql("ALTER TABLE spaces ADD COLUMN cluster_self VARCHAR(500) NOT NULL DEFAULT ''")
        kits = conn.exec_driver_sql("PRAGMA table_info(gedmatch_kits)").fetchall()
        if kits and not any(row[1] == "sex" for row in kits):
            conn.exec_driver_sql("ALTER TABLE gedmatch_kits ADD COLUMN sex VARCHAR(1) NOT NULL DEFAULT ''")
        hits = conn.exec_driver_sql("PRAGMA table_info(gedmatch_hits)").fetchall()
        if hits and not any(row[1] == "date_compared" for row in hits):
            conn.exec_driver_sql(
                "ALTER TABLE gedmatch_hits ADD COLUMN date_compared VARCHAR(32) NOT NULL DEFAULT ''"
            )
        users = conn.exec_driver_sql("PRAGMA table_info(users)").fetchall()
        if users and not any(row[1] == "must_set_password" for row in users):
            conn.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN must_set_password INTEGER NOT NULL DEFAULT 0"
            )
        users = conn.exec_driver_sql("PRAGMA table_info(users)").fetchall()
        if users and not any(row[1] == "email" for row in users):
            conn.exec_driver_sql("ALTER TABLE users ADD COLUMN email VARCHAR(255) NOT NULL DEFAULT ''")
        if users:
            conn.exec_driver_sql(
                "UPDATE users SET email = ? WHERE username = ? AND email = ''",
                (KINSHIP_EDITOR_EMAIL, "ruslan"),
            )
        members = conn.exec_driver_sql("PRAGMA table_info(cluster_members)").fetchall()
        member_columns = {
            "kit": "VARCHAR(64) NOT NULL DEFAULT ''",
            "segments": "INTEGER",
            "largest_cm": "FLOAT",
            "icw": "INTEGER",
            "relationship": "VARCHAR(120) NOT NULL DEFAULT ''",
            "tree_url": "VARCHAR(1000) NOT NULL DEFAULT ''",
            "tree_count": "INTEGER",
            "note": "VARCHAR(500) NOT NULL DEFAULT ''",
        }
        present = {row[1] for row in members}
        for name, ddl in member_columns.items():
            if members and name not in present:
                conn.exec_driver_sql(f"ALTER TABLE cluster_members ADD COLUMN {name} {ddl}")
        _seed_ruslan_matrix(conn)


def _seed_ruslan_matrix(conn) -> None:
    space = conn.exec_driver_sql("SELECT id FROM spaces WHERE id = 1").fetchone()
    if space is None:
        return
    existing = conn.exec_driver_sql(
        "SELECT id FROM gedmatch_matrices WHERE space_id = 1 AND name = 'Ruslan Aharodnik'"
    ).fetchone()
    if existing is not None:
        return
    conn.exec_driver_sql(
        "INSERT INTO gedmatch_matrices (space_id, name, receives_unlisted) "
        "VALUES (1, 'Ruslan Aharodnik', 1)"
    )
    matrix_id = conn.exec_driver_sql("SELECT last_insert_rowid()").scalar()
    conn.exec_driver_sql(
        "INSERT INTO gedmatch_matrix_kits (matrix_id, kit) "
        "SELECT ?, source_kit FROM gedmatch_hits WHERE space_id = 1 GROUP BY source_kit",
        (matrix_id,),
    )
