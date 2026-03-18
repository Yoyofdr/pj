"""
Gestión del filesystem de salida:
- Estructura: out/<Sala>/YYYY/
    <caso>.txt        ← texto completo
    <caso>.json       ← metadatos
    CorteSuprema/     ← PDFs por instancia
    CorteApelaciones/
    Tribunales/
- Límite de 10.000 archivos directos por carpeta año.
- Versionado: YYYY (2), YYYY (3), etc.
"""

from __future__ import annotations

from pathlib import Path

from src.logger import get_logger
from src.utils import sanitize_filename

log = get_logger()

MAX_FILES_PER_DIR = 10_000

# Mapeo de sección interna a nombre de carpeta
SECTION_DIR_MAP = {
    "SUPREMA": "CorteSuprema",
    "APELACIONES": "CorteApelaciones",
    "TRIBUNALES": "Tribunales",
}


def _count_files(directory: Path) -> int:
    """Cuenta archivos (no directorios) en un directorio."""
    if not directory.exists():
        return 0
    return sum(1 for f in directory.iterdir() if f.is_file())


def get_safe_dir(
    base_dir: Path,
    year: str,
    max_files: int = MAX_FILES_PER_DIR,
) -> Path:
    """
    Retorna directorio YYYY que no exceda max_files.
    Si YYYY tiene >= max_files, crea YYYY (2), YYYY (3), etc.
    """
    # Primera opción: YYYY
    candidate = base_dir / year
    candidate.mkdir(parents=True, exist_ok=True)

    if _count_files(candidate) < max_files:
        return candidate

    # Buscar siguiente versión disponible
    version = 2
    while True:
        versioned = base_dir / f"{year} ({version})"
        versioned.mkdir(parents=True, exist_ok=True)
        if _count_files(versioned) < max_files:
            log.debug("Usando directorio versionado: %s", versioned)
            return versioned
        version += 1
        if version > 1000:  # Safety valve
            log.error("Demasiadas versiones de directorio para %s", year)
            return versioned


def normalize_sala_name(sala: str | None) -> str:
    """
    Normaliza sala a nombre seguro de carpeta.
    """
    if not sala:
        return "SIN_SALA"
    clean = sanitize_filename(sala, max_len=80)
    return clean or "SIN_SALA"


def _get_year_dir(output_dir: Path, sala: str | None, year: str) -> Path:
    """
    Directorio base para una sala+año: out/<Sala>/<YYYY>/
    Aplica límite de 10k archivos directos.
    """
    sala_name = normalize_sala_name(sala)
    return get_safe_dir(output_dir / sala_name, year)


def get_text_dir(output_dir: Path, sala: str | None, year: str) -> Path:
    """out/<Sala>/<YYYY>/ — donde se guardan los .txt de sentencias."""
    return _get_year_dir(output_dir, sala, year)


def get_meta_dir(output_dir: Path, sala: str | None, year: str) -> Path:
    """out/<Sala>/<YYYY>/ — donde se guardan los .json de metadatos."""
    return _get_year_dir(output_dir, sala, year)


def get_download_dir(
    output_dir: Path,
    section: str,
    sala: str | None,
    year: str,
) -> Path:
    """
    out/<Sala>/<YYYY>/<Seccion>/ — PDFs/docs de cada instancia.
    La subcarpeta de sección se crea bajo el directorio año.
    """
    section_name = SECTION_DIR_MAP.get(section, section)
    year_dir = _get_year_dir(output_dir, sala, year)
    section_dir = year_dir / section_name
    section_dir.mkdir(parents=True, exist_ok=True)
    return section_dir
