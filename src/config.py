"""Configuración centralizada del scraper via dataclass."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    """Todos los flags y defaults del scraper."""

    # ── Navegador ──────────────────────────────────────────────
    headful: bool = True
    persist_session: bool = True
    storage_state_path: Path = field(default_factory=lambda: Path("./state/storage.json"))

    # ── Base de datos ──────────────────────────────────────────
    db_path: Path = field(default_factory=lambda: Path("./state/manifest.sqlite"))

    # ── Output ─────────────────────────────────────────────────
    output_dir: Path = field(default_factory=lambda: Path("./out"))

    # ── Rate limiting / reintentos ─────────────────────────────
    rate_limit_seconds: float = 1.0
    fast_mode: bool = False
    retries: int = 6
    timeout_ms: int = 60_000

    # ── Paginación ─────────────────────────────────────────────
    start_page: int = 1
    max_pages: int | None = None
    max_items: int | None = None
    target_year: int | None = None

    # ── Resumen / retry ────────────────────────────────────────
    retry_failed: bool = False
    only_missing: bool = True

    # ── Secciones a descargar ──────────────────────────────────
    download_suprema: bool = True
    download_apelaciones: bool = True
    download_tribunales: bool = True

    # ── Modo test ──────────────────────────────────────────────
    test_mode: bool = False

    # ── Modo CI (GitHub Actions u otro entorno no-interactivo) ─
    ci_mode: bool = False

    # ── URL base ───────────────────────────────────────────────
    base_url: str = "https://juris.pjud.cl/busqueda?Corte_Suprema"

    # ── Logs ───────────────────────────────────────────────────
    logs_dir: Path = field(default_factory=lambda: Path("./logs"))

    def __post_init__(self) -> None:
        # Convertir strings a Path si vienen del CLI
        for fname in ("storage_state_path", "db_path", "output_dir", "logs_dir"):
            val = getattr(self, fname)
            if isinstance(val, str):
                setattr(self, fname, Path(val))

        if self.test_mode:
            self.max_pages = self.max_pages or 1
            self.max_items = self.max_items or 5

    @property
    def dumps_dir(self) -> Path:
        return self.logs_dir / "dumps"

    def sections_enabled(self) -> list[str]:
        """Devuelve lista de secciones habilitadas."""
        sections: list[str] = []
        if self.download_suprema:
            sections.append("SUPREMA")
        if self.download_apelaciones:
            sections.append("APELACIONES")
        if self.download_tribunales:
            sections.append("TRIBUNALES")
        return sections


def _bool_flag(v: str) -> bool:
    if v.lower() in ("true", "1", "yes", "si", "sí"):
        return True
    if v.lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"Valor booleano inválido: {v}")


def parse_args(argv: list[str] | None = None) -> Config:
    """Parsea argumentos CLI y retorna Config."""
    p = argparse.ArgumentParser(
        prog="pjud-scraper",
        description="Descargador de sentencias del Poder Judicial de Chile (juris.pjud.cl)",
    )

    p.add_argument("--headful", type=_bool_flag, default=True,
                    help="Ejecutar navegador con ventana visible (default: true)")
    p.add_argument("--persist-session", type=_bool_flag, default=True,
                    help="Persistir cookies/sesión entre ejecuciones (default: true)")
    p.add_argument("--storage-state-path", type=str, default="./state/storage.json",
                    help="Ruta al archivo de estado de sesión")
    p.add_argument("--db-path", type=str, default="./state/manifest.sqlite",
                    help="Ruta a la base de datos SQLite")
    p.add_argument("--output-dir", type=str, default="./out",
                    help="Directorio de salida para documentos")
    p.add_argument("--rate-limit-seconds", type=float, default=1.0,
                    help="Segundos entre procesamiento de cada caso")
    p.add_argument("--fast-mode", type=_bool_flag, default=False,
                    help="Reduce esperas fijas para mayor velocidad (default: false)")
    p.add_argument("--retries", type=int, default=6,
                    help="Reintentos máximos por descarga")
    p.add_argument("--timeout-ms", type=int, default=60_000,
                    help="Timeout en milisegundos para operaciones de Playwright")
    p.add_argument("--start-page", type=int, default=1,
                    help="Página desde la que comenzar")
    p.add_argument("--max-pages", type=int, default=None,
                    help="Máximo de páginas a procesar")
    p.add_argument("--max-items", type=int, default=None,
                    help="Máximo de casos a procesar")
    p.add_argument("--target-year", type=int, default=None,
                    help="Procesar solo causas de este año (ej: 2025)")
    p.add_argument("--retry-failed", type=_bool_flag, default=False,
                    help="Reintentar casos FAILED (default: false)")
    p.add_argument("--only-missing", type=_bool_flag, default=True,
                    help="Solo procesar casos sin descargas completas (default: true)")
    p.add_argument("--download-suprema", type=_bool_flag, default=True,
                    help="Descargar sección Corte Suprema")
    p.add_argument("--download-apelaciones", type=_bool_flag, default=True,
                    help="Descargar sección Corte Apelaciones")
    p.add_argument("--download-tribunales", type=_bool_flag, default=True,
                    help="Descargar sección Tribunales")
    p.add_argument("--test-mode", action="store_true", default=False,
                    help="Modo test: max-pages=1, max-items=5")
    p.add_argument("--ci-mode", action="store_true", default=False,
                    help="Modo CI: falla de inmediato si no hay sesión activa (sin pedir login manual)")
    p.add_argument("--logs-dir", type=str, default="./logs",
                    help="Directorio de logs")

    args = p.parse_args(argv)
    return Config(**vars(args))
