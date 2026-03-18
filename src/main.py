"""
CLI principal del scraper de sentencias judiciales (juris.pjud.cl).

Uso:
    python -m src.main [flags]
    python -m src.main --test-mode
    python -m src.main --start-page 3 --max-pages 5
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

from src.config import Config, parse_args
from src.logger import setup_logger, get_logger


async def run(config: Config) -> None:
    """Flujo principal del scraper."""
    log = get_logger()
    base_pace = config.rate_limit_seconds
    pace = max(0.05, base_pace * (0.25 if config.fast_mode else 1.0))

    # Importaciones diferidas para que el logger ya esté configurado
    from playwright.async_api import async_playwright
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

    from src.auth import (
        check_session_alive,
        detect_logged_in,
        handle_session_expired,
        wait_for_manual_login,
    )
    from src.case_page import process_case_page, save_case_text, _close_case_detail
    from src.db import Database
    from src.listing import (
        apply_year_filter,
        set_results_per_page,
        _extract_cases_from_listing,
        _go_to_next_page,
    )

    # ── Inicializar DB ─────────────────────────────────────────
    db = Database(config.db_path)
    log.info("Base de datos: %s", config.db_path)
    log.info("Output dir: %s", config.output_dir)
    log.info("Secciones habilitadas: %s", config.sections_enabled())
    log.info("Ritmo: base=%.2fs, efectivo=%.2fs, fast_mode=%s", base_pace, pace, config.fast_mode)

    if config.test_mode:
        log.info("🧪 MODO TEST activo (max_pages=%s, max_items=%s)", config.max_pages, config.max_items)

    # ── Crear directorios ──────────────────────────────────────
    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    config.dumps_dir.mkdir(parents=True, exist_ok=True)
    config.storage_state_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Lanzar Playwright ──────────────────────────────────────
    log.info("Iniciando navegador (headful=%s)...", config.headful)

    async with async_playwright() as pw:
        # Opciones de lanzamiento
        launch_args = {
            "headless": not config.headful,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        }

        browser = await pw.chromium.launch(**launch_args)

        # Contexto con storage state si existe
        context_args: dict = {
            "accept_downloads": True,
            "viewport": {"width": 1366, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        if config.persist_session and config.storage_state_path.exists():
            try:
                context_args["storage_state"] = str(config.storage_state_path)
                log.info("Cargando sesión persistida desde %s", config.storage_state_path)
            except Exception as e:
                log.warning("No se pudo cargar storage state: %s", e)

        context = await browser.new_context(**context_args)
        page = await context.new_page()

        # Timeout por defecto
        page.set_default_timeout(config.timeout_ms)

        try:
            # ── FASE 1: Navegar a la página de búsqueda ────────
            log.info("Navegando a %s", config.base_url)
            await page.goto(config.base_url, wait_until="domcontentloaded", timeout=config.timeout_ms)
            await asyncio.sleep(0.5)

            # ── FASE 2: Verificar autenticación ────────────────
            log.info("Verificando autenticación...")
            is_logged = await detect_logged_in(page)

            if not is_logged:
                if config.ci_mode:
                    log.error("=" * 60)
                    log.error("❌  SESIÓN INVÁLIDA — MODO CI")
                    log.error("=" * 60)
                    log.error("  No hay sesión activa de ClaveÚnica.")
                    log.error("  En modo CI no es posible hacer login manual.")
                    log.error("")
                    log.error("  Solución: ejecuta el scraper localmente, inicia sesión,")
                    log.error("  y actualiza el secreto SESSION_JSON en GitHub con el")
                    log.error("  contenido de state/storage.json (en base64).")
                    log.error("")
                    log.error("  Comando para exportar la sesión:")
                    log.error("    base64 -i state/storage.json | tr -d '\\n'")
                    log.error("=" * 60)
                    sys.exit(1)

                log.info("No se detectó sesión activa.")
                is_logged = await wait_for_manual_login(
                    page=page,
                    context=context,
                    storage_state_path=config.storage_state_path if config.persist_session else None,
                    base_url=config.base_url,
                )

                # Asegurar que estamos en la página de búsqueda
                if "busqueda" not in page.url:
                    await page.goto(config.base_url, wait_until="domcontentloaded", timeout=config.timeout_ms)
                    await asyncio.sleep(0.5)
            else:
                log.info("✅ Sesión activa detectada")
                # Guardar estado
                if config.persist_session:
                    try:
                        await context.storage_state(path=str(config.storage_state_path))
                    except Exception:
                        pass

            # ── FASE 3: Aplicar filtro por año (si corresponde) ─
            target_year = str(config.target_year) if config.target_year else None
            year_filter_applied = False
            if config.target_year:
                year_filter_applied = await apply_year_filter(page, config.target_year, config)

            # ── FASE 4: Setear 50 resultados por página ────────
            set_ok = await set_results_per_page(page, config)
            if not set_ok:
                # El sitio a veces queda en estado transitorio con 0 resultados.
                # Forzamos una recarga del listado antes de continuar.
                try:
                    await page.evaluate(
                        """() => {
                            if (typeof window.cargar_datos_resultados_busqueda_sentencias === 'function') {
                                window.cargar_datos_resultados_busqueda_sentencias();
                            }
                        }"""
                    )
                    await asyncio.sleep(0.5)
                except Exception:
                    pass

            # ── FASE ÚNICA: Listar y procesar en el acto ───────
            log.info("=" * 60)
            log.info("FASE ÚNICA: Listando y descargando en línea...")
            log.info("=" * 60)

            # Asegurar que estamos en la página de búsqueda
            if "busqueda" not in page.url:
                await page.goto(config.base_url, wait_until="domcontentloaded", timeout=config.timeout_ms)
                await asyncio.sleep(0.5)

            # Si se pide partir en otra página, avanzar antes de procesar
            current_page_num = config.start_page
            if config.start_page > 1:
                log.info("Navegando a página inicial %d...", config.start_page)
                for _ in range(config.start_page - 1):
                    advanced = await _go_to_next_page(page, config)
                    if not advanced:
                        log.warning("No se pudo avanzar hasta la página %d", config.start_page)
                        break
                    await asyncio.sleep(pace)

            processed = 0
            failed_total = 0
            listed_total = 0
            session_check_interval = 25 if config.fast_mode else 10
            pages_processed = 0
            stop_due_to_max = False

            async def _goto_page_direct(page_num: int) -> bool:
                """
                Salta directamente a una página del listado SPA.
                page_num es 1-indexed.
                """
                if page_num < 1:
                    return False
                try:
                    ok = await page.evaluate(
                        """(n) => {
                            const idx = Number(n) - 1;
                            if (!Number.isFinite(idx) || idx < 0) return false;
                            if (typeof window.cargar_datos_resultados_busqueda_sentencias !== 'function') return false;
                            window.pagina_resultados_busqueda_sentencias = idx;
                            window.cargar_datos_resultados_busqueda_sentencias();
                            return true;
                        }""",
                        page_num,
                    )
                    if not ok:
                        return False
                    await asyncio.sleep(0.8)
                    for _ in range(6):
                        cases_probe = await _extract_cases_from_listing(page)
                        if cases_probe:
                            return True
                        await asyncio.sleep(0.3)
                    return False
                except Exception:
                    return False

            def _years_from_cases(cases_data: list[dict]) -> list[int]:
                years: list[int] = []
                for c in cases_data:
                    raw = c.get("fecha_sentencia") or ""
                    if len(raw) >= 4 and raw[:4].isdigit():
                        years.append(int(raw[:4]))
                return years

            async def _seek_first_page_for_year(target: int) -> int | None:
                """
                Encuentra (aprox.) la primera página que contiene target year usando
                salto directo + búsqueda binaria sobre páginas.
                """
                try:
                    total_results = await page.evaluate(
                        """() => {
                            const v = Number(window.cantidad_global_resultados_busqueda_sentencias);
                            return Number.isFinite(v) ? v : 0;
                        }"""
                    )
                    if not total_results or total_results <= 0:
                        return None
                    total_pages = max(1, (int(total_results) + 49) // 50)
                except Exception:
                    return None

                lo, hi = 1, total_pages
                found: int | None = None

                while lo <= hi:
                    mid = (lo + hi) // 2
                    if not await _goto_page_direct(mid):
                        return None
                    mid_cases = await _extract_cases_from_listing(page)
                    yrs = _years_from_cases(mid_cases)
                    if not yrs:
                        return None

                    min_y, max_y = min(yrs), max(yrs)
                    if min_y <= target <= max_y:
                        found = mid
                        hi = mid - 1
                    elif min_y > target:
                        lo = mid + 1
                    else:  # max_y < target
                        hi = mid - 1

                if found is None:
                    return None

                # Ajuste fino hacia la izquierda para ubicar el primer bloque del año.
                page_num = found
                while page_num > 1:
                    prev = page_num - 1
                    if not await _goto_page_direct(prev):
                        break
                    prev_cases = await _extract_cases_from_listing(page)
                    yrs = _years_from_cases(prev_cases)
                    if yrs and (min(yrs) <= target <= max(yrs)):
                        page_num = prev
                        continue
                    break
                return page_num

            async def _reset_and_reposition(target_page_num: int) -> bool:
                """
                Recuperación fuerte del listado:
                1) volver a la búsqueda base
                2) reconfigurar resultados por página
                3) avanzar hasta target_page_num
                """
                try:
                    await page.goto(config.base_url, wait_until="domcontentloaded", timeout=config.timeout_ms)
                    await asyncio.sleep(0.5)
                    if config.target_year:
                        await apply_year_filter(page, config.target_year, config)
                    await set_results_per_page(page, config)
                    await asyncio.sleep(0.5)

                    if target_page_num > 1:
                        for _ in range(target_page_num - 1):
                            advanced = await _go_to_next_page(page, config)
                            if not advanced:
                                return False
                            await asyncio.sleep(0.5)
                    return True
                except Exception:
                    return False

            # ── MODO PARALELO: workers independientes ──────────────
            if config.concurrency > 1:
                log.info("=" * 60)
                log.info("MODO PARALELO: %d workers concurrentes", config.concurrency)
                log.info("=" * 60)

                from src.worker import case_worker

                # Exportar cookies del contexto autenticado para los workers
                storage_state_dict = await context.storage_state()

                case_queue: asyncio.Queue = asyncio.Queue(maxsize=config.concurrency * 6)
                stop_event = asyncio.Event()
                counters = {"ok": 0, "fail": 0, "processed": 0, "skipped": 0}

                # Feeder: pagina el listado y encola casos
                async def _feeder() -> None:
                    nonlocal listed_total, pages_processed, stop_due_to_max, current_page_num
                    feeder_processed = 0

                    # Ubicar año si aplica
                    if (
                        target_year
                        and not year_filter_applied
                        and pages_processed == 0
                        and current_page_num == config.start_page
                    ):
                        try:
                            target_int = int(target_year)
                        except Exception:
                            target_int = 0
                        if target_int > 0:
                            seek_page = await _seek_first_page_for_year(target_int)
                            if seek_page:
                                current_page_num = seek_page
                                log.info("Año %s encontrado cerca de página %d.", target_year, seek_page)

                    while True:
                        log.info("─── [Feeder] Página %d ───", current_page_num)
                        feed_cases = await _extract_cases_from_listing(page)

                        if not feed_cases:
                            recovered = False
                            for attempt in range(1, 4):
                                log.warning("Feeder: página %d vacía (intento %d/3)", current_page_num, attempt)
                                try:
                                    await page.evaluate(
                                        """() => {
                                            if (typeof window.cargar_datos_resultados_busqueda_sentencias === 'function')
                                                window.cargar_datos_resultados_busqueda_sentencias();
                                        }"""
                                    )
                                except Exception:
                                    pass
                                await asyncio.sleep(2 + attempt)
                                feed_cases = await _extract_cases_from_listing(page)
                                if feed_cases:
                                    recovered = True
                                    break
                            if not recovered:
                                reset_ok = await _reset_and_reposition(current_page_num)
                                if reset_ok:
                                    feed_cases = await _extract_cases_from_listing(page)
                                    if not feed_cases:
                                        log.info("Feeder: no hay más casos. Fin.")
                                        break
                                else:
                                    log.info("Feeder: no hay más casos. Fin.")
                                    break

                        listed_total += len(feed_cases)

                        # Filtrar por año si aplica
                        if target_year:
                            feed_cases = [c for c in feed_cases if (c.get("fecha_sentencia") or "").startswith(target_year)]

                        for feed_case in feed_cases:
                            fid = feed_case["case_id"]
                            db.upsert_case(
                                case_id=fid,
                                url=feed_case["url"],
                                rol=feed_case.get("rol"),
                                fecha_sentencia=feed_case.get("fecha_sentencia"),
                                caratulado=feed_case.get("caratulado"),
                                corte_origen=feed_case.get("corte_origen"),
                                page_fingerprint=None,
                            )
                            if feed_case.get("idsentencia"):
                                db.set_setting(f"idsentencia_{fid}", feed_case["idsentencia"])

                            existing_feed = db.get_case(fid)
                            if existing_feed and existing_feed["status"] == "DONE":
                                counters["skipped"] += 1
                                continue

                            dup = db.find_done_duplicate_case_id(
                                rol=feed_case.get("rol"),
                                fecha_sentencia=feed_case.get("fecha_sentencia"),
                                caratulado=feed_case.get("caratulado"),
                                sala=feed_case.get("sala"),
                                exclude_case_id=fid,
                            )
                            if dup:
                                db.set_case_status(fid, "DONE", error=f"DUPLICATE_OF:{dup}")
                                counters["skipped"] += 1
                                continue

                            if config.max_items and feeder_processed >= config.max_items:
                                stop_due_to_max = True
                                break

                            await case_queue.put(feed_case)
                            feeder_processed += 1

                        if stop_due_to_max:
                            break

                        pages_processed += 1
                        if config.max_pages and pages_processed >= config.max_pages:
                            log.info("Feeder: alcanzado max_pages=%d", config.max_pages)
                            break

                        advanced = await _go_to_next_page(page, config)
                        if not advanced:
                            next_pn = current_page_num + 1
                            reset_ok = await _reset_and_reposition(next_pn)
                            if not reset_ok:
                                log.info("Feeder: no hay más páginas.")
                                break
                            current_page_num = next_pn
                        else:
                            current_page_num += 1
                        await asyncio.sleep(pace)

                    stop_event.set()
                    log.info("Feeder terminado. Encolados: %d", feeder_processed)

                # Lanzar workers
                worker_tasks = []
                for wid in range(1, config.concurrency + 1):
                    t = asyncio.create_task(
                        case_worker(
                            worker_id=wid,
                            browser=browser,
                            storage_state=storage_state_dict,
                            queue=case_queue,
                            db=db,
                            config=config,
                            counters=counters,
                            stop_event=stop_event,
                        )
                    )
                    worker_tasks.append(t)

                # Correr feeder y esperar workers
                await _feeder()
                await case_queue.join()           # esperar que todos los items sean procesados
                await asyncio.gather(*worker_tasks)

                processed = counters["processed"]
                failed_total = counters["fail"]
                log.info(
                    "Modo paralelo completo: %d procesados, %d FAILED, %d saltados",
                    processed, failed_total, counters["skipped"],
                )

            else:
                # ── MODO SECUENCIAL (original) ──────────────────────
                # fmt: off
                while True:
                    # Si no se pudo aplicar filtro nativo por año, intentar búsqueda
                    # directa de bloque para evitar barrido lineal completo.
                    if (
                        target_year
                        and not year_filter_applied
                        and pages_processed == 0
                        and current_page_num == config.start_page
                    ):
                        try:
                            target_int = int(target_year)
                        except Exception:
                            target_int = 0
                        if target_int > 0:
                            seek_page = await _seek_first_page_for_year(target_int)
                            if seek_page:
                                current_page_num = seek_page
                                log.info("Año %s encontrado cerca de página %d.", target_year, seek_page)
                            else:
                                log.warning("No se pudo ubicar directamente el año %s. Continuando lineal.", target_year)

                    log.info("─── Página %d ───", current_page_num)
                    cases = await _extract_cases_from_listing(page)
                    if not cases:
                        # Reintento defensivo: el listado puede quedar vacío temporalmente.
                        recovered = False
                        for attempt in range(1, 4):
                            log.warning(
                                "Página %d sin casos (intento %d/3). Reintentando recarga de listado...",
                                current_page_num,
                                attempt,
                            )
                            try:
                                await page.evaluate(
                                    """() => {
                                        if (typeof window.cargar_datos_resultados_busqueda_sentencias === 'function') {
                                            window.cargar_datos_resultados_busqueda_sentencias();
                                        }
                                    }"""
                                )
                            except Exception:
                                pass
                            await asyncio.sleep(2 + attempt)
                            cases = await _extract_cases_from_listing(page)
                            if cases:
                                recovered = True
                                log.info("Página %d recuperada con %d casos.", current_page_num, len(cases))
                                break

                        if not recovered:
                            log.warning(
                                "Página %d sigue vacía tras reintentos. Intentando reset de búsqueda...",
                                current_page_num,
                            )
                            reset_ok = await _reset_and_reposition(current_page_num)
                            if reset_ok:
                                cases = await _extract_cases_from_listing(page)
                                if cases:
                                    recovered = True
                                    log.info(
                                        "Página %d recuperada tras reset con %d casos.",
                                        current_page_num,
                                        len(cases),
                                    )

                        if not recovered:
                            log.info("No se encontraron casos en esta página. Fin.")
                            break

                    listed_total += len(cases)
                    # Filtrado opcional por año objetivo
                    cases_to_process = cases
                    if target_year:
                        year_matches = [
                            c for c in cases
                            if (c.get("fecha_sentencia") or "").startswith(target_year)
                        ]
                        skipped_non_target = len(cases) - len(year_matches)
                        if skipped_non_target:
                            log.info(
                                "Página %d: %d caso(s) fuera de %s, se omiten.",
                                current_page_num,
                                skipped_non_target,
                                target_year,
                            )
                        cases_to_process = year_matches

                    for case in cases_to_process:
                        case_id = case["case_id"]
                        case_url = case["url"]
                        idsentencia = case.get("idsentencia")

                        db.upsert_case(
                            case_id=case_id,
                            url=case_url,
                            rol=case.get("rol"),
                            fecha_sentencia=case.get("fecha_sentencia"),
                            caratulado=case.get("caratulado"),
                            corte_origen=case.get("corte_origen"),
                            page_fingerprint=None,
                        )
                        if idsentencia:
                            db.set_setting(f"idsentencia_{case_id}", idsentencia)

                        # Saltar casos ya procesados en corridas previas.
                        existing_case = db.get_case(case_id)
                        if existing_case and existing_case["status"] == "DONE":
                            log.info("Caso %s ya estaba DONE. Se omite.", case_id[:12])
                            continue

                        # Dedupe por identidad de negocio (mismo fallo, distinto idsentencia/case_id).
                        dup_case_id = db.find_done_duplicate_case_id(
                            rol=case.get("rol"),
                            fecha_sentencia=case.get("fecha_sentencia"),
                            caratulado=case.get("caratulado"),
                            sala=case.get("sala"),
                            exclude_case_id=case_id,
                        )
                        if dup_case_id:
                            db.set_case_status(case_id, "DONE", error=f"DUPLICATE_OF:{dup_case_id}")
                            log.info(
                                "Caso %s duplicado de %s (rol/fecha/caratulado). Se omite.",
                                case_id[:12],
                                dup_case_id[:12],
                            )
                            continue

                        if config.max_items and processed >= config.max_items:
                            log.info("Alcanzado max_items=%d. Deteniendo procesamiento.", config.max_items)
                            stop_due_to_max = True
                            break

                        # ── Verificación periódica de sesión ───────────
                        if processed > 0 and processed % session_check_interval == 0:
                            still_logged = await check_session_alive(page)
                            if not still_logged:
                                if config.ci_mode:
                                    log.error("=" * 60)
                                    log.error("❌  SESIÓN EXPIRADA — MODO CI")
                                    log.error("=" * 60)
                                    log.error("  La sesión de ClaveÚnica expiró durante el scraping.")
                                    log.error("  Actualiza el secreto SESSION_JSON en GitHub.")
                                    log.error("=" * 60)
                                    sys.exit(1)
                                log.warning("Sesión posiblemente expirada")
                                relogged = await handle_session_expired(
                                    page=page,
                                    context=context,
                                    storage_state_path=(
                                        config.storage_state_path if config.persist_session else None
                                    ),
                                    base_url=config.base_url,
                                )
                                if not relogged:
                                    log.warning("Continuando sin sesión activa...")

                        db.set_case_status(case_id, "IN_PROGRESS")
                        log.info(
                            "━━━ Caso %d [%s] idsentencia=%s ━━━",
                            processed + 1,
                            case_id[:12],
                            idsentencia or "?",
                        )

                        async def _process_single_case() -> None:
                            case_data = await process_case_page(
                                page, case_id, case_url, db, config,
                                idsentencia=idsentencia,
                            )

                            metadata = case_data.get("metadata", {})
                            fecha = metadata.get("fecha_sentencia")
                            sala = metadata.get("sala")
                            txt_path = save_case_text(case_data, config.output_dir, fecha, sala)

                            db.set_case_status(case_id, "DONE")
                            if txt_path:
                                log.info("  ✅ Guardado: %s", txt_path.name)

                        try:
                            await asyncio.wait_for(
                                _process_single_case(), timeout=120,
                            )

                            processed += 1
                            log.info(
                                "  Caso %d/%s procesado",
                                processed, "?" if not config.max_items else config.max_items,
                            )

                        except asyncio.TimeoutError:
                            log.error(
                                "⏱️  Caso %s excedió timeout de 120s. Saltando.",
                                case_id[:12],
                            )
                            db.set_case_status(case_id, "FAILED", error="TIMEOUT_120s")
                            failed_total += 1
                            processed += 1

                        except Exception as e:
                            log.error("Error procesando caso %s: %s", case_id[:12], e, exc_info=True)
                            db.set_case_status(case_id, "FAILED", error=str(e)[:500])
                            failed_total += 1
                            processed += 1

                            try:
                                dumps = config.dumps_dir
                                dumps.mkdir(parents=True, exist_ok=True)
                                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                                await page.screenshot(
                                    path=str(dumps / f"case_error_{case_id[:12]}_{ts}.png"),
                                    full_page=True,
                                )
                            except Exception:
                                pass

                        try:
                            await _close_case_detail(page, config)
                            await asyncio.sleep(0.5)
                        except Exception:
                            try:
                                await page.goto(
                                    config.base_url, wait_until="domcontentloaded", timeout=config.timeout_ms
                                )
                                await asyncio.sleep(0.3)
                            except Exception:
                                pass

                        await asyncio.sleep(pace)

                    pages_processed += 1
                    log.info(
                        "Página %d: %d casos listados (acumulado listado: %d, procesados: %d)",
                        current_page_num,
                        len(cases),
                        listed_total,
                        processed,
                    )

                    if stop_due_to_max:
                        break
                    if config.max_pages and pages_processed >= config.max_pages:
                        log.info("Alcanzado max_pages=%d", config.max_pages)
                        break

                    advanced = await _go_to_next_page(page, config)
                    if not advanced:
                        # Recuperación fuerte antes de declarar fin.
                        next_page_num = current_page_num + 1
                        log.warning(
                            "No se pudo avanzar a página %d. Intentando reset y reposicionamiento...",
                            next_page_num,
                        )
                        reset_ok = await _reset_and_reposition(next_page_num)
                        if not reset_ok:
                            log.info("No hay más páginas. Fin.")
                            break
                        current_page_num = next_page_num
                        await asyncio.sleep(pace)
                        continue
                    current_page_num += 1
                    await asyncio.sleep(pace)

            # ── Guardar sesión final ───────────────────────────
            if config.persist_session:
                try:
                    await context.storage_state(path=str(config.storage_state_path))
                    log.info("Estado de sesión guardado")
                except Exception as e:
                    log.warning("No se pudo guardar sesión: %s", e)

        except KeyboardInterrupt:
            log.info("Interrumpido por el usuario (Ctrl+C)")
        except Exception as e:
            log.error("Error fatal: %s", e, exc_info=True)
        finally:
            # ── Diagnósticos finales ───────────────────────────
            diag = db.diagnostics()
            log.info("=" * 60)
            log.info("RESUMEN FINAL")
            log.info("=" * 60)
            log.info("  Casos por estado: %s", diag["cases"])
            log.info("  Descargas por estado: %s", diag["downloads"])
            log.info("  Total casos: %d", diag["total_cases"])
            log.info("  Total descargas: %d", diag["total_downloads"])
            log.info("=" * 60)

            # Cerrar
            db.close()
            await context.close()
            await browser.close()


def diagnostics_cmd(config: Config) -> None:
    """Imprime diagnósticos sin abrir navegador."""
    from src.db import Database

    log = get_logger()

    if not config.db_path.exists():
        log.info("No existe base de datos en %s", config.db_path)
        return

    db = Database(config.db_path)
    diag = db.diagnostics()

    log.info("=" * 50)
    log.info("DIAGNÓSTICO")
    log.info("=" * 50)
    log.info("  DB: %s", config.db_path)
    log.info("  Casos por estado: %s", diag["cases"])
    log.info("  Descargas por estado: %s", diag["downloads"])
    log.info("  Total casos: %d", diag["total_cases"])
    log.info("  Total descargas: %d", diag["total_downloads"])
    log.info("=" * 50)

    db.close()


def main(argv: list[str] | None = None) -> None:
    """Entry point CLI."""
    config = parse_args(argv)
    setup_logger(config.logs_dir)
    log = get_logger()

    log.info("pjud-scraper iniciado")
    log.info("Config: headful=%s, persist=%s, test=%s", config.headful, config.persist_session, config.test_mode)
    log.info("  rate_limit=%.1fs, fast_mode=%s, retries=%d, timeout=%dms",
             config.rate_limit_seconds, config.fast_mode, config.retries, config.timeout_ms)
    log.info("  start_page=%d, max_pages=%s, max_items=%s", config.start_page, config.max_pages, config.max_items)
    log.info("  retry_failed=%s, only_missing=%s", config.retry_failed, config.only_missing)
    log.info("  sections: suprema=%s, apelaciones=%s, tribunales=%s",
             config.download_suprema, config.download_apelaciones, config.download_tribunales)
    log.info("  concurrency=%d", config.concurrency)

    start = time.time()

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        log.info("Interrumpido por el usuario")
    finally:
        elapsed = time.time() - start
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        log.info("Tiempo total: %dm %ds", mins, secs)


if __name__ == "__main__":
    main()
