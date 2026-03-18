"""
Worker para procesamiento paralelo de casos.

Cada worker tiene su propio BrowserContext con cookies compartidas del contexto principal.
Recibe casos de una asyncio.Queue y los procesa de forma independiente.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

from src.config import Config
from src.logger import get_logger

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Browser
    from src.db import Database

log = get_logger()


async def _setup_worker_page(context: "BrowserContext", config: Config):
    """Navega la página del worker a la URL base y la deja lista."""
    from src.listing import apply_year_filter, set_results_per_page

    page = await context.new_page()
    page.set_default_timeout(config.timeout_ms)

    await page.goto(config.base_url, wait_until="domcontentloaded", timeout=config.timeout_ms)
    await asyncio.sleep(0.5)

    if config.target_year:
        await apply_year_filter(page, config.target_year, config)

    await set_results_per_page(page, config)
    return page


async def case_worker(
    worker_id: int,
    browser: "Browser",
    storage_state: dict,
    queue: asyncio.Queue,
    db: "Database",
    config: Config,
    counters: dict,
    stop_event: asyncio.Event,
) -> None:
    """
    Worker que consume casos de la queue y los procesa en su propio contexto.
    """
    from playwright.async_api import Error as PlaywrightError
    from src.case_page import process_case_page, save_case_text, _close_case_detail

    log.info("[Worker %d] Iniciando...", worker_id)

    context = await browser.new_context(
        storage_state=storage_state,
        accept_downloads=True,
        viewport={"width": 1366, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()
    page.set_default_timeout(config.timeout_ms)

    try:
        # Navegar a la URL base (necesario para que el SPA cargue sus funciones JS)
        await page.goto(config.base_url, wait_until="domcontentloaded", timeout=config.timeout_ms)
        await asyncio.sleep(0.5)

        while not (stop_event.is_set() and queue.empty()):
            try:
                case = queue.get_nowait()
            except asyncio.QueueEmpty:
                if stop_event.is_set():
                    break
                await asyncio.sleep(0.1)
                continue

            case_id = case["case_id"]
            case_url = case["url"]
            idsentencia = case.get("idsentencia")

            # Verificar si ya está procesado
            existing = db.get_case(case_id)
            if existing and existing["status"] == "DONE":
                log.info("[W%d] Caso %s ya DONE. Saltando.", worker_id, case_id[:12])
                queue.task_done()
                counters["skipped"] += 1
                continue

            db.set_case_status(case_id, "IN_PROGRESS")
            log.info("[W%d] ━━━ Caso %s idsentencia=%s ━━━", worker_id, case_id[:12], idsentencia or "?")

            try:
                await asyncio.wait_for(
                    _process_and_download(page, context, case_id, case_url, idsentencia, db, config, worker_id),
                    timeout=120,
                )
                counters["ok"] += 1
                counters["processed"] += 1
                db.set_case_status(case_id, "DONE")
                log.info("[W%d] ✅ Caso %s procesado", worker_id, case_id[:12])

            except asyncio.TimeoutError:
                log.error("[W%d] Caso %s excedió timeout 120s", worker_id, case_id[:12])
                db.set_case_status(case_id, "FAILED", error="TIMEOUT_120s")
                counters["fail"] += 1
                counters["processed"] += 1

            except Exception as e:
                log.error("[W%d] Error en caso %s: %s", worker_id, case_id[:12], e, exc_info=True)
                db.set_case_status(case_id, "FAILED", error=str(e)[:500])
                counters["fail"] += 1
                counters["processed"] += 1

                try:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    config.dumps_dir.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(
                        path=str(config.dumps_dir / f"w{worker_id}_error_{case_id[:12]}_{ts}.png"),
                        full_page=True,
                    )
                except Exception:
                    pass

            finally:
                # Intentar cerrar el detalle y volver al listado
                try:
                    await _close_case_detail(page, config)
                except Exception:
                    pass
                # Asegurar que seguimos en la URL base (necesaria para abrir detalles SPA)
                try:
                    if "busqueda" not in page.url:
                        await page.goto(config.base_url, wait_until="domcontentloaded", timeout=config.timeout_ms)
                        await asyncio.sleep(0.3)
                except Exception:
                    pass

                queue.task_done()

            # Pequeña pausa de cortesía entre casos
            pace = max(0.05, config.rate_limit_seconds * (0.25 if config.fast_mode else 1.0))
            await asyncio.sleep(pace)

    except Exception as e:
        log.error("[Worker %d] Error fatal: %s", worker_id, e, exc_info=True)
    finally:
        try:
            await context.close()
        except Exception:
            pass
        log.info("[Worker %d] Finalizado. Procesados: %d", worker_id, counters.get("processed", 0))


async def _process_and_download(
    page,
    context,
    case_id: str,
    case_url: str,
    idsentencia: str | None,
    db: "Database",
    config: Config,
    worker_id: int,
) -> None:
    """Procesa un caso: extrae texto y guarda .txt."""
    from src.case_page import process_case_page, save_case_text

    case_data = await process_case_page(
        page, case_id, case_url, db, config, idsentencia=idsentencia
    )

    metadata = case_data.get("metadata", {})
    fecha = metadata.get("fecha_sentencia")
    sala = metadata.get("sala")
    txt_path = save_case_text(case_data, config.output_dir, fecha, sala)
    if txt_path:
        log.info("[Worker %d] ✅ %s", worker_id, txt_path.name)
