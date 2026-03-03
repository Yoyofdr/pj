"""
Gestión del filesystem de salida:
- Estructura: out/<Sección>/<Sala>/YYYY/
- Límite de 10.000 archivos por carpeta.
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


def get_download_dir(
    output_dir: Path,
    section: str,
    sala: str | None,
    year: str,
) -> Path:
    """
    Retorna el directorio de descarga para una sección/sala/año.
    Aplica límite de 10k archivos.
    """
    section_name = SECTION_DIR_MAP.get(section, section)
    sala_name = normalize_sala_name(sala)
    base = output_dir / section_name / sala_name
    return get_safe_dir(base, year)


def get_meta_dir(output_dir: Path, sala: str | None, year: str) -> Path:
    """Retorna directorio para metadatos JSON por sala/año."""
    sala_name = normalize_sala_name(sala)
    return get_safe_dir(output_dir / "meta" / sala_name, year)


def get_text_dir(output_dir: Path, sala: str | None, year: str) -> Path:
    """Retorna directorio para archivos unificados de causa por sala/año."""
    sala_name = normalize_sala_name(sala)
    return get_safe_dir(output_dir / "sentencias" / sala_name, year)
