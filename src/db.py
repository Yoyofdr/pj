"""SQLite: schema, helpers y operaciones CRUD."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

from src.logger import get_logger

# ── Schema SQL ─────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id            TEXT PRIMARY KEY,
    url                TEXT NOT NULL,
    rol                TEXT,
    fecha_sentencia    TEXT,            -- YYYY-MM-DD nullable
    caratulado         TEXT,
    corte_origen       TEXT,
    sala               TEXT,
    materias           TEXT,            -- Materias del caso (para filtrar/ordenar)
    recurso            TEXT,
    resultado_recurso  TEXT,
    status             TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING/IN_PROGRESS/DONE/FAILED
    error              TEXT,
    page_fingerprint   TEXT,
    last_seen          TEXT
);

CREATE TABLE IF NOT EXISTS downloads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id         TEXT NOT NULL REFERENCES cases(case_id),
    section         TEXT NOT NULL,       -- SUPREMA/APELACIONES/TRIBUNALES
    label           TEXT NOT NULL,       -- sentencia_original, anexo_001, …
    source          TEXT,                -- button/link/newtab/url
    url             TEXT,
    filename        TEXT,
    filepath        TEXT,
    sha256          TEXT,
    bytes           INTEGER,
    status          TEXT NOT NULL DEFAULT 'OK',  -- OK/FAILED/SKIPPED
    error           TEXT,
    downloaded_at   TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status);
CREATE INDEX IF NOT EXISTS idx_downloads_case ON downloads(case_id);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
"""


class Database:
    """Wrapper SQLite con helpers para el scraper."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self.log = get_logger()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Agrega columnas nuevas si no existen (para DBs pre-existentes)."""
        existing = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(cases)").fetchall()
        }
        new_cols = {
            "sala": "TEXT",
            "materias": "TEXT",
            "recurso": "TEXT",
            "resultado_recurso": "TEXT",
        }
        for col, typ in new_cols.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE cases ADD COLUMN {col} {typ}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        cur = self.conn.cursor()
        try:
            yield cur
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ── Cases ──────────────────────────────────────────────────

    def upsert_case(
        self,
        case_id: str,
        url: str,
        rol: str | None = None,
        fecha_sentencia: str | None = None,
        caratulado: str | None = None,
        corte_origen: str | None = None,
        sala: str | None = None,
        materias: str | None = None,
        recurso: str | None = None,
        resultado_recurso: str | None = None,
        page_fingerprint: str | None = None,
    ) -> None:
        now = datetime.now().isoformat()
        self.conn.execute(
            """
            INSERT INTO cases (case_id, url, rol, fecha_sentencia, caratulado,
                               corte_origen, sala, materias, recurso, resultado_recurso,
                               status, page_fingerprint, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
            ON CONFLICT(case_id) DO UPDATE SET
                url = excluded.url,
                rol = COALESCE(excluded.rol, cases.rol),
                fecha_sentencia = COALESCE(excluded.fecha_sentencia, cases.fecha_sentencia),
                caratulado = COALESCE(excluded.caratulado, cases.caratulado),
                corte_origen = COALESCE(excluded.corte_origen, cases.corte_origen),
                sala = COALESCE(excluded.sala, cases.sala),
                materias = COALESCE(excluded.materias, cases.materias),
                recurso = COALESCE(excluded.recurso, cases.recurso),
                resultado_recurso = COALESCE(excluded.resultado_recurso, cases.resultado_recurso),
                page_fingerprint = COALESCE(excluded.page_fingerprint, cases.page_fingerprint),
                last_seen = excluded.last_seen
            """,
            (case_id, url, rol, fecha_sentencia, caratulado, corte_origen,
             sala, materias, recurso, resultado_recurso, page_fingerprint, now),
        )
        self.conn.commit()

    def set_case_status(self, case_id: str, status: str, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE cases SET status = ?, error = ? WHERE case_id = ?",
            (status, error, case_id),
        )
        self.conn.commit()

    def get_case(self, case_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()

    def find_done_duplicate_case_id(
        self,
        rol: str | None,
        fecha_sentencia: str | None,
        caratulado: str | None,
        sala: str | None,
        exclude_case_id: str | None = None,
    ) -> str | None:
        """
        Busca un caso DONE equivalente por identidad de negocio.
        Útil para evitar re-procesar variantes duplicadas del mismo fallo.
        """
        if not rol or not fecha_sentencia or not caratulado:
            return None

        params: tuple
        query = """
            SELECT case_id
            FROM cases
            WHERE status = 'DONE'
              AND rol = ?
              AND fecha_sentencia = ?
              AND caratulado = ?
              AND COALESCE(sala, '') = COALESCE(?, '')
        """
        if exclude_case_id:
            query += " AND case_id != ?"
            params = (rol, fecha_sentencia, caratulado, sala, exclude_case_id)
        else:
            params = (rol, fecha_sentencia, caratulado, sala)

        row = self.conn.execute(query + " LIMIT 1", params).fetchone()
        return row["case_id"] if row else None

    def get_pending_cases(self, include_failed: bool = False) -> list[sqlite3.Row]:
        if include_failed:
            return self.conn.execute(
                "SELECT * FROM cases WHERE status IN ('PENDING', 'FAILED') ORDER BY rowid"
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM cases WHERE status = 'PENDING' ORDER BY rowid"
        ).fetchall()

    def count_cases_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM cases GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    # ── Downloads ──────────────────────────────────────────────

    def insert_download(
        self,
        case_id: str,
        section: str,
        label: str,
        source: str | None = None,
        url: str | None = None,
        filename: str | None = None,
        filepath: str | None = None,
        sha256: str | None = None,
        bytes_: int | None = None,
        status: str = "OK",
        error: str | None = None,
    ) -> int:
        now = datetime.now().isoformat()
        cur = self.conn.execute(
            """
            INSERT INTO downloads
                (case_id, section, label, source, url, filename, filepath,
                 sha256, bytes, status, error, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (case_id, section, label, source, url, filename, filepath,
             sha256, bytes_, status, error, now),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore

    def download_exists(self, case_id: str, section: str, label: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM downloads
            WHERE case_id = ? AND section = ? AND label = ? AND status = 'OK'
            LIMIT 1
            """,
            (case_id, section, label),
        ).fetchone()
        return row is not None

    def get_downloads_for_case(self, case_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM downloads WHERE case_id = ? ORDER BY id",
            (case_id,),
        ).fetchall()

    def count_downloads_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM downloads GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    # ── Settings ───────────────────────────────────────────────

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    # ── Diagnostics ────────────────────────────────────────────

    def diagnostics(self) -> dict[str, Any]:
        return {
            "cases": self.count_cases_by_status(),
            "downloads": self.count_downloads_by_status(),
            "total_cases": self.conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0],
            "total_downloads": self.conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0],
        }
