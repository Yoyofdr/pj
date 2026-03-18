"""
Descargador robusto de documentos:
- Primer intento: page.expect_download() tras click en botón/link.
- Fallback A: si abre nueva pestaña con PDF inline → capturar URL + context.request.
- Fallback B: si navega a URL de archivo → context.request.
- Detección de content-type y content-disposition.
- Screenshots y HTML dumps en caso de fallo.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page, BrowserContext, Download, Error as PlaywrightError

from src.config import Config
from src.db import Database
from src.filesystem import get_download_dir
from src.logger import get_logger
from src.utils import fecha_to_ym, sanitize_filename, sha256_file

log = get_logger()


async def _save_debug(page: Page, dumps_dir: Path, label: str) -> None:
    dumps_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        await page.screenshot(path=str(dumps_dir / f"{label}_{ts}.png"), full_page=True)
    except Exception:
        pass
    try:
        html = await page.content()
        (dumps_dir / f"{label}_{ts}.html").write_text(html, encoding="utf-8")
    except Exception:
        pass


def _extract_filename_from_disposition(header: str | None) -> str | None:
    """Extrae filename de Content-Disposition header."""
    if not header:
        return None
    # filename*=UTF-8''name.pdf
    m = re.search(r"filename\*=(?:UTF-8''|utf-8'')(.+?)(?:;|$)", header, re.IGNORECASE)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1).strip())
    # filename="name.pdf" o filename=name.pdf
    m = re.search(r'filename="?([^";]+)"?', header, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


async def _try_expect_download(
    page: Page,
    element,
    timeout_ms: int,
) -> Download | None:
    """Intento 1: click + expect_download."""
    try:
        async with page.expect_download(timeout=timeout_ms) as download_info:
            await element.click()
        download = await download_info.value
        return download
    except PlaywrightError as e:
        log.debug("expect_download falló: %s", e)
        return None
    except Exception as e:
        log.debug("expect_download error inesperado: %s", e)
        return None


async def _try_new_tab_capture(
    page: Page,
    context: BrowserContext,
    element,
    dest_path: Path,
    timeout_ms: int,
) -> Path | None:
    """Fallback A: capturar PDF inline abierto en nueva pestaña."""
    try:
        # Escuchar nueva página
        async with context.expect_page(timeout=timeout_ms) as new_page_info:
            await element.click()

        new_page = await new_page_info.value
        await new_page.wait_for_load_state("load", timeout=timeout_ms)
        await asyncio.sleep(1)

        url = new_page.url
        log.debug("Nueva pestaña abierta: %s", url)

        # Si es un PDF inline o un archivo descargable
        if url and not url.startswith("about:"):
            # Descargar via API request del contexto (con cookies)
            response = await context.request.get(url)
            if response.ok:
                body = await response.body()
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_bytes(body)

                # Intentar cerrar la pestaña
                try:
                    await new_page.close()
                except Exception:
                    pass

                return dest_path

        try:
            await new_page.close()
        except Exception:
            pass

    except PlaywrightError as e:
        log.debug("Captura de nueva pestaña falló: %s", e)
    except Exception as e:
        log.debug("Error en captura de nueva pestaña: %s", e)

    return None


async def _try_url_download(
    context: BrowserContext,
    url: str,
    dest_path: Path,
) -> tuple[Path | None, str | None]:
    """Fallback B: descargar directamente de URL con context.request."""
    if not url or url.startswith("javascript:") or url.startswith("#"):
        return None, None

    try:
        # Asegurar URL absoluta
        if url.startswith("/"):
            url = f"https://juris.pjud.cl{url}"

        response = await context.request.get(url)
        if not response.ok:
            log.debug("URL download HTTP %d: %s", response.status, url)
            return None, None

        # Detectar filename desde Content-Disposition
        content_disp = response.headers.get("content-disposition")
        suggested_name = _extract_filename_from_disposition(content_disp)

        body = await response.body()
        if not body or len(body) < 100:
            log.debug("Respuesta vacía o muy pequeña para %s", url)
            return None, suggested_name

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(body)
        return dest_path, suggested_name

    except Exception as e:
        log.debug("URL download falló para %s: %s", url, e)
        return None, None


async def _try_navigation_download(
    page: Page,
    context: BrowserContext,
    element,
    dest_path: Path,
    timeout_ms: int,
) -> Path | None:
    """Fallback C: el click navega la página actual a un archivo."""
    original_url = page.url
    try:
        await element.click()
        await asyncio.sleep(2)

        new_url = page.url
        if new_url != original_url and new_url != "about:blank":
            # Verificar si es un archivo
            content_type = ""
            try:
                resp = await context.request.head(new_url)
                content_type = resp.headers.get("content-type", "")
            except Exception:
                pass

            if any(t in content_type for t in ("pdf", "octet-stream", "msword", "zip")):
                response = await context.request.get(new_url)
                if response.ok:
                    body = await response.body()
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    dest_path.write_bytes(body)

                    # Volver a la página original
                    await page.goto(original_url, wait_until="networkidle", timeout=timeout_ms)
                    return dest_path

        # Si no fue un archivo, volver
        if page.url != original_url:
            await page.goto(original_url, wait_until="networkidle", timeout=timeout_ms)

    except Exception as e:
        log.debug("Navigation download falló: %s", e)
        try:
            if page.url != original_url:
                await page.goto(original_url, wait_until="networkidle", timeout=timeout_ms)
        except Exception:
            pass

    return None


def _determine_extension(filename: str | None, suggested: str | None) -> str:
    """Determina extensión del archivo."""
    for name in (filename, suggested):
        if name and "." in name:
            ext = name.rsplit(".", 1)[-1].lower()
            if ext in ("pdf", "doc", "docx", "zip", "rar", "xlsx", "xls", "odt"):
                return f".{ext}"
    return ".pdf"  # Default


async def download_item(
    page: Page,
    context: BrowserContext,
    case_id: str,
    section: str,
    item: dict,
    fecha: str | None,
    sala: str | None,
    db: Database,
    config: Config,
) -> bool:
    """
    Descarga un elemento individual, probando múltiples estrategias.
    Registra resultado en la DB.
    Retorna True si fue exitoso.
    """
    label = item["label"]
    element = item.get("element")
    source_url = item.get("url")
    source_type = item.get("source", "unknown")

    # ── Verificar si ya existe ─────────────────────────────────
    if db.download_exists(case_id, section, label):
        log.debug("Skip (ya existe): %s/%s/%s", case_id[:12], section, label)
        return True

    year, month = fecha_to_ym(fecha)
    download_dir = get_download_dir(config.output_dir, section, sala, year)
    download_dir.mkdir(parents=True, exist_ok=True)

    # Nombre temporal
    safe_label = sanitize_filename(label)
    temp_name = f"{case_id[:12]}_{safe_label}"

    log.info("  Descargando: [%s] %s", section, label)

    saved_path: Path | None = None
    final_filename: str | None = None
    source_method: str = source_type

    # ── Estrategia 1: URL directa (más rápido, sin interacción de browser) ───
    if source_url and not source_url.startswith("javascript:") and source_url != "#":
        ext = _determine_extension(source_url, None)
        temp_dest = download_dir / f"{temp_name}{ext}"

        result, suggested = await _try_url_download(context, source_url, temp_dest)
        if result:
            source_method = "url"
            final_filename = suggested or f"{temp_name}{ext}"
            final_filename = sanitize_filename(final_filename)

            final_dest = download_dir / final_filename
            if final_dest != result:
                counter = 1
                while final_dest.exists():
                    stem = final_dest.stem
                    final_dest = download_dir / f"{stem}_{counter}{final_dest.suffix}"
                    counter += 1
                result.rename(final_dest)
                saved_path = final_dest
            else:
                saved_path = result
            log.info("  ✅ Descargado (URL directa): %s", final_filename)

    # ── Estrategia 2: expect_download (click en botón/link) ────
    if not saved_path and element:
        download = await _try_expect_download(page, element, config.timeout_ms)
        if download:
            source_method = "button"
            suggested = download.suggested_filename
            ext = _determine_extension(suggested, None)
            final_filename = suggested or f"{temp_name}{ext}"
            final_filename = sanitize_filename(final_filename)

            dest = download_dir / final_filename
            # Evitar colisiones
            counter = 1
            while dest.exists():
                stem = dest.stem
                dest = download_dir / f"{stem}_{counter}{dest.suffix}"
                counter += 1

            try:
                await download.save_as(str(dest))
                saved_path = dest
                log.info("  ✅ Descargado (expect_download): %s", final_filename)
            except Exception as e:
                log.warning("  save_as falló: %s", e)
                try:
                    tmp = await download.path()
                    if tmp:
                        shutil.copy2(str(tmp), str(dest))
                        saved_path = dest
                except Exception:
                    pass

    # ── Estrategia 3: Captura de nueva pestaña ─────────────────
    if not saved_path and element:
        ext = _determine_extension(None, None)
        temp_dest = download_dir / f"{temp_name}{ext}"

        result = await _try_new_tab_capture(page, context, element, temp_dest, config.timeout_ms)
        if result:
            source_method = "newtab"
            saved_path = result
            final_filename = result.name
            log.info("  ✅ Descargado (nueva pestaña): %s", final_filename)

    # ── Estrategia 4: Navegación directa ───────────────────────
    if not saved_path and element:
        ext = _determine_extension(None, None)
        temp_dest = download_dir / f"{temp_name}{ext}"

        result = await _try_navigation_download(page, context, element, temp_dest, config.timeout_ms)
        if result:
            source_method = "navigation"
            saved_path = result
            final_filename = result.name
            log.info("  ✅ Descargado (navegación): %s", final_filename)

    # ── Registrar resultado ────────────────────────────────────
    if saved_path and saved_path.exists():
        file_hash = sha256_file(saved_path)
        file_size = saved_path.stat().st_size

        db.insert_download(
            case_id=case_id,
            section=section,
            label=label,
            source=source_method,
            url=source_url,
            filename=final_filename or saved_path.name,
            filepath=str(saved_path),
            sha256=file_hash,
            bytes_=file_size,
            status="OK",
        )
        return True
    else:
        error_msg = "Todas las estrategias de descarga fallaron"
        log.warning("  ❌ FAILED: %s/%s/%s - %s", case_id[:12], section, label, error_msg)

        # Guardar debug info
        await _save_debug(page, config.dumps_dir, f"download_failed_{case_id[:12]}_{section}_{safe_label}")

        db.insert_download(
            case_id=case_id,
            section=section,
            label=label,
            source=source_method,
            url=source_url,
            filename=None,
            filepath=None,
            sha256=None,
            bytes_=None,
            status="FAILED",
            error=error_msg,
        )
        return False


async def download_case_documents(
    page: Page,
    context: BrowserContext,
    case_data: dict,
    db: Database,
    config: Config,
) -> tuple[int, int]:
    """
    Descarga todos los documentos de un caso procesado.
    Retorna (exitosos, fallidos).
    """
    case_id = case_data["case_id"]
    metadata = case_data.get("metadata", {})
    fecha = metadata.get("fecha_sentencia")
    sala = metadata.get("sala")
    downloadables = case_data.get("_downloadables", {})

    ok_count = 0
    fail_count = 0

    for section, items in downloadables.items():
        if not items:
            # Registrar SKIPPED para sección vacía
            if not db.download_exists(case_id, section, "none"):
                db.insert_download(
                    case_id=case_id,
                    section=section,
                    label="none",
                    source=None,
                    status="SKIPPED",
                    error="No hay elementos descargables en esta sección",
                )
            continue

        async def _dl_one(item: dict) -> bool:
            try:
                return await download_item(
                    page=page,
                    context=context,
                    case_id=case_id,
                    section=section,
                    item=item,
                    fecha=fecha,
                    sala=sala,
                    db=db,
                    config=config,
                )
            except Exception as e:
                log.error("Error descargando %s/%s: %s", section, item["label"], e)
                db.insert_download(
                    case_id=case_id,
                    section=section,
                    label=item["label"],
                    source=item.get("source"),
                    url=item.get("url"),
                    status="FAILED",
                    error=str(e),
                )
                return False

        # Items con URL conocida se descargan en paralelo (HTTP directo);
        # los que requieren click en browser se procesan uno a uno para
        # no interferir con el estado de la página.
        url_items = [it for it in items if it.get("url") and not (it.get("url") or "").startswith("javascript:")]
        btn_items = [it for it in items if it not in url_items]

        if url_items:
            results = await asyncio.gather(*[_dl_one(it) for it in url_items])
            for r in results:
                if r:
                    ok_count += 1
                else:
                    fail_count += 1

        for item in btn_items:
            r = await _dl_one(item)
            if r:
                ok_count += 1
            else:
                fail_count += 1

    return ok_count, fail_count
