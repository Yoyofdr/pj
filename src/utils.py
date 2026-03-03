"""Utilidades genéricas: hashing, sanitización de filenames, etc."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from pathlib import Path


def sha1(text: str) -> str:
    """SHA-1 hex de un string UTF-8."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    """SHA-256 hex de un archivo (lectura por bloques)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def sanitize_filename(name: str, max_len: int = 200) -> str:
    """
    Limpia un string para usarlo como nombre de archivo.
    - Remueve caracteres ilegales.
    - Normaliza unicode.
    - Trunca a max_len.
    """
    # Normalizar unicode
    name = unicodedata.normalize("NFKD", name)
    # Reemplazar separadores de directorio y caracteres problemáticos
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # Colapsar espacios y guiones bajos múltiples
    name = re.sub(r"[_\s]+", "_", name)
    name = name.strip("_. ")
    # Truncar
    if len(name) > max_len:
        ext = ""
        if "." in name:
            parts = name.rsplit(".", 1)
            if len(parts[1]) <= 10:
                ext = "." + parts[1]
                name = parts[0]
        name = name[: max_len - len(ext)] + ext
    return name or "unnamed"


def parse_fecha(text: str | None) -> str | None:
    """
    Intenta parsear una fecha en formato chileno a YYYY-MM-DD.
    Soporta formatos:
    - DD/MM/YYYY
    - DD-MM-YYYY
    - YYYY-MM-DD (ya normalizado)
    """
    if not text:
        return None
    text = text.strip()

    # Ya en formato ISO
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text

    # DD/MM/YYYY o DD-MM-YYYY
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", text)
    if m:
        d, mo, y = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return None

    return None


def current_ym() -> tuple[str, str]:
    """Retorna (YYYY, MM) actual."""
    now = datetime.now()
    return now.strftime("%Y"), now.strftime("%m")


def fecha_to_ym(fecha: str | None) -> tuple[str, str]:
    """Extrae (YYYY, MM) de fecha YYYY-MM-DD. Si None, usa fecha actual."""
    if fecha and re.match(r"^\d{4}-\d{2}-\d{2}$", fecha):
        parts = fecha.split("-")
        return parts[0], parts[1]
    return current_ym()


def fingerprint_page(case_ids: list[str], count: int) -> str:
    """
    Crea fingerprint de página para detectar loops.
    fp = sha1(first_3 + last_3 + count)
    """
    first3 = case_ids[:3]
    last3 = case_ids[-3:] if len(case_ids) >= 3 else case_ids
    raw = "|".join(first3) + "||" + "|".join(last3) + "||" + str(count)
    return sha1(raw)
