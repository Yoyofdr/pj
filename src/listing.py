"""
Paginación del listado de búsqueda:
- Setear 50 resultados por página via select#resultados_busqueda_registros_por_pagina.
- Navegar páginas con ul.pagination.
- Anti-loop con fingerprint.
- Extraer data-idsentencia de cada span.estilo_resultado_titulo.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page

from src.config import Config
from src.db import Database
from src.logger import get_logger
from src.selectors import (
    RESULT_ITEM,
    RESULTS_PER_PAGE_SELECT_ID,
    PAGINATION_NEXT,
    PAGINATION_ACTIVE,
)
from src.utils import fingerprint_page, sha1, parse_fecha

log = get_logger()


async def _save_debug(page: Page, dumps_dir: Path, label: str) -> None:
    """Guarda screenshot + HTML para debug."""
    dumps_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        await page.screenshot(path=str(dumps_dir / f"{label}_{ts}.png"), full_page=True)
    except Exception as e:
        log.debug("No se pudo guardar screenshot: %s", e)
    try:
        html = await page.content()
        (dumps_dir / f"{label}_{ts}.html").write_text(html, encoding="utf-8")
    except Exception as e:
        log.debug("No se pudo guardar HTML: %s", e)


async def set_results_per_page(page: Page, config: Config) -> bool:
    """
    Configura 50 resultados por página.
    Usa el select nativo con id fijo + dispatchEvent('change').
    """
    log.info("Configurando 50 resultados por página...")

    try:
        # Verificar que existe el select
        sel = await page.query_selector(f"#{RESULTS_PER_PAGE_SELECT_ID}")
        if not sel:
            log.warning("Select de resultados por página no encontrado")
            await _save_debug(page, config.dumps_dir, "select_not_found")
            return False

        # Leer valor actual
        current = await page.evaluate(
            f"document.getElementById('{RESULTS_PER_PAGE_SELECT_ID}').value"
        )
        log.info("Valor actual de resultados por página: %s", current)

        if current == "50":
            log.info("Ya está en 50 resultados por página")
            return True

        # Contar resultados antes del cambio
        count_before = await page.evaluate(f"""(() => {{
            const spans = document.querySelectorAll('{RESULT_ITEM}');
            const seen = new Set();
            spans.forEach(s => seen.add(s.getAttribute('data-idsentencia')));
            return seen.size;
        }})()""")
        log.info("Resultados antes del cambio: %d", count_before)

        # Cambiar a 50 con JS + disparar evento change
        await page.evaluate(f"""(() => {{
            const sel = document.getElementById('{RESULTS_PER_PAGE_SELECT_ID}');
            sel.value = '50';
            sel.dispatchEvent(new Event('change', {{bubbles: true}}));
        }})()""")

        # Esperar a que se carguen nuevos resultados (polling en vez de sleep fijo)
        log.info("Esperando recarga de resultados...")
        for _w in range(30):
            await asyncio.sleep(0.3)
            _probe = await page.evaluate(f"""(() => {{
                const spans = document.querySelectorAll('{RESULT_ITEM}');
                const seen = new Set();
                spans.forEach(s => seen.add(s.getAttribute('data-idsentencia')));
                return seen.size;
            }})()""")
            if _probe > 0:
                break

        # Verificar que cambió
        count_after = await page.evaluate(f"""(() => {{
            const spans = document.querySelectorAll('{RESULT_ITEM}');
            const seen = new Set();
            spans.forEach(s => seen.add(s.getAttribute('data-idsentencia')));
            return seen.size;
        }})()""")
        log.info("Resultados después del cambio: %d", count_after)

        if count_after > count_before:
            log.info("✅ Configurado a 50 resultados por página (%d → %d)", count_before, count_after)
            return True
        elif count_after > 0:
            log.info("Resultados cargados: %d (puede que no haya 50 disponibles)", count_after)
            return True
        else:
            log.warning("0 resultados después de cambiar select. Reintentando...")
            # Esperar más
            await asyncio.sleep(5)
            count_retry = await page.evaluate(f"""(() => {{
                const spans = document.querySelectorAll('{RESULT_ITEM}');
                const seen = new Set();
                spans.forEach(s => seen.add(s.getAttribute('data-idsentencia')));
                return seen.size;
            }})()""")
            if count_retry > 0:
                log.info("Resultados tras espera adicional: %d", count_retry)
                return True

            log.warning("No se pudieron cargar resultados con 50 por página")
            await _save_debug(page, config.dumps_dir, "set_50_failed")
            return False

    except Exception as e:
        log.warning("Error seteando 50 resultados: %s", e)
        await _save_debug(page, config.dumps_dir, "set_50_error")
        return False


async def apply_year_filter(page: Page, year: int, config: Config) -> bool:
    """
    Aplica filtro de año usando el formulario nativo (fecha desde/hasta)
    y ejecuta la búsqueda del sitio.
    """
    log.info("Aplicando filtro de año en origen: %s", year)
    try:
        year_s = str(int(year))
    except Exception:
        log.warning("Año inválido para filtro: %r", year)
        return False

    start_date = f"{year_s}-01-01"
    end_date = f"{year_s}-12-31"

    try:
        await page.evaluate(
            """({startDate, endDate}) => {
                const from = document.getElementById('fec_desde');
                const to = document.getElementById('fec_hasta');
                if (from) from.value = startDate;
                if (to) to.value = endDate;

                if (typeof window.btn_buscar_componente_busqueda_avanzada_click === 'function') {
                    window.btn_buscar_componente_busqueda_avanzada_click();
                    return true;
                }

                if (typeof window.get_filtros_busqueda === 'function'
                    && typeof window.cargar_datos_resultados_busqueda_sentencias === 'function') {
                    window.pagina_resultados_busqueda_sentencias = 0;
                    const filtros = window.get_filtros_busqueda();
                    window.cargar_datos_resultados_busqueda_sentencias(filtros);
                    return true;
                }
                return false;
            }""",
            {"startDate": start_date, "endDate": end_date},
        )
    except Exception as e:
        log.warning("Error disparando búsqueda filtrada por año: %s", e)
        await _save_debug(page, config.dumps_dir, "year_filter_apply_error")
        return False

    # Esperar recarga del listado
    await asyncio.sleep(0.5)
    for _ in range(25):
        count = await page.evaluate(
            f"""() => {{
                const spans = document.querySelectorAll('{RESULT_ITEM}');
                const seen = new Set();
                spans.forEach(s => {{
                    const id = s.getAttribute('data-idsentencia');
                    if (id) seen.add(id);
                }});
                return seen.size;
            }}"""
        )
        if count > 0:
            break
        await asyncio.sleep(0.35)

    # Verificación básica: que los inputs quedaron seteados.
    vals = await page.evaluate(
        """() => ({
            desde: (document.getElementById('fec_desde') || {}).value || '',
            hasta: (document.getElementById('fec_hasta') || {}).value || '',
        })"""
    )
    ok = vals.get("desde") == start_date and vals.get("hasta") == end_date
    if ok:
        log.info("✅ Filtro de año aplicado: %s (%s a %s)", year_s, start_date, end_date)
    else:
        log.warning("El filtro de año no quedó persistido en los controles: %s", vals)
    return ok


async def _extract_cases_from_listing(page: Page) -> list[dict]:
    """
    Extrae información de cada caso visible en el listado.
    Cada caso es un span.estilo_resultado_titulo[data-idsentencia].
    """
    cases: list[dict] = []

    results = await page.evaluate(f"""(() => {{
        const spans = document.querySelectorAll('{RESULT_ITEM}');
        const seen = new Set();
        const res = [];

        spans.forEach(s => {{
            const idsentencia = s.getAttribute('data-idsentencia');
            if (!idsentencia || seen.has(idsentencia)) return;
            seen.add(idsentencia);

            const titleText = s.innerText?.trim() || '';

            // Buscar la card padre para extraer metadatos
            const card = s.closest('.card') || s.closest('.card-header')?.parentElement;
            let cardText = '';
            if (card) {{
                cardText = card.innerText?.trim() || '';
            }} else {{
                // Fallback: ir al div padre más cercano con texto largo
                let parent = s.parentElement;
                while (parent && (!parent.innerText || parent.innerText.length < 100)) {{
                    parent = parent.parentElement;
                }}
                cardText = parent ? parent.innerText?.trim() || '' : '';
            }}

            res.push({{
                idsentencia,
                titleText: titleText.substring(0, 200),
                cardText: cardText.substring(0, 600)
            }});
        }});

        return res;
    }})()""")

    for r in results:
        idsentencia = r["idsentencia"]
        case_id = sha1(f"pjud_{idsentencia}")
        title = r["titleText"]
        card_text = r["cardText"]

        # Extraer metadatos del texto del card
        rol = None
        fecha = None
        caratulado = None

        # ROL: viene en el título "Rol: XXXXX-YYYY"
        rol_match = re.search(r"Rol[:\s]+(\d{1,7}-\d{4})", title)
        if rol_match:
            rol = rol_match.group(1)

        # Fecha: buscar en el card text
        fecha_match = re.search(r"Fecha[^:]*:\s*(\d{1,2}-\d{1,2}-\d{4})", card_text)
        if fecha_match:
            fecha = parse_fecha(fecha_match.group(1))

        # Caratulado: buscar en card text
        carat_match = re.search(r"Caratulado[:\s]+([^\n]+)", card_text)
        if carat_match:
            caratulado = carat_match.group(1).strip()[:300]

        cases.append({
            "case_id": case_id,
            "idsentencia": idsentencia,
            "url": f"https://juris.pjud.cl/busqueda?Corte_Suprema#sentencia_{idsentencia}",
            "rol": rol,
            "fecha_sentencia": fecha,
            "caratulado": caratulado,
            "corte_origen": None,
        })

    return cases


async def _go_to_next_page(page: Page, config: Config) -> bool:
    """
    Navega a la siguiente página usando el botón ▶ de la paginación.
    Retorna True si se avanzó.
    """
    try:
        def _norm_page(txt: str | None) -> str:
            # El paginador trae non-breaking spaces alrededor del número.
            return (txt or "").replace("\xa0", " ").strip()

        async def _snapshot_state() -> tuple[str, int | None, str]:
            active_el = await page.query_selector(PAGINATION_ACTIVE)
            active_text = _norm_page(await active_el.inner_text() if active_el else "?")
            page_var = await page.evaluate(
                """() => {
                    const v = Number(window.pagina_resultados_busqueda_sentencias);
                    return Number.isFinite(v) ? v : null;
                }"""
            )
            first_id = await page.evaluate(
                f"""() => {{
                    const el = document.querySelector('{RESULT_ITEM}');
                    return el?.getAttribute('data-idsentencia') || '';
                }}"""
            )
            return active_text, page_var, first_id

        current_page_text, current_page_var, current_first_id = await _snapshot_state()

        # Buscar botón siguiente (▶)
        next_btn = await page.query_selector(PAGINATION_NEXT)
        if not next_btn:
            log.info("No se encontró botón ▶ de siguiente página")
            return False

        # Verificar si está deshabilitado
        parent_li = await next_btn.evaluate_handle("el => el.closest('li')")
        if parent_li:
            is_disabled = await parent_li.as_element().evaluate(
                "el => el.classList.contains('disabled')"
            )
            if is_disabled:
                log.info("Botón siguiente está deshabilitado (última página)")
                return False

        async def _wait_page_change(
            previous_text: str,
            previous_var: int | None,
            previous_first_id: str,
            timeout_ms: int = 12_000,
        ) -> bool:
            end = asyncio.get_running_loop().time() + (timeout_ms / 1000.0)
            while asyncio.get_running_loop().time() < end:
                new_page_text, new_page_var, new_first_id = await _snapshot_state()
                if (
                    (new_page_text and new_page_text != previous_text)
                    or (previous_var is not None and new_page_var is not None and new_page_var != previous_var)
                    or (new_first_id and previous_first_id and new_first_id != previous_first_id)
                ):
                    log.debug(
                        "Página cambió: txt %s→%s, var %s→%s",
                        previous_text,
                        new_page_text,
                        previous_var,
                        new_page_var,
                    )
                    return True
                await asyncio.sleep(0.35)
            return False

        async def _wait_results_ready(timeout_ms: int = 12_000) -> bool:
            end = asyncio.get_running_loop().time() + (timeout_ms / 1000.0)
            while asyncio.get_running_loop().time() < end:
                count = await page.evaluate(
                    f"""() => {{
                        const els = document.querySelectorAll('{RESULT_ITEM}');
                        let ok = 0;
                        els.forEach(el => {{
                            if (el.getAttribute('data-idsentencia')) ok += 1;
                        }});
                        return ok;
                    }}"""
                )
                if count > 0:
                    return True
                await asyncio.sleep(0.35)
            return False

        # Estrategia determinística SPA:
        # avanzar 1 página modificando el índice global y recargando resultados.
        advanced_via_js = await page.evaluate(
            """() => {
                const current = Number(window.pagina_resultados_busqueda_sentencias);
                const hasFn = typeof window.cargar_datos_resultados_busqueda_sentencias === 'function';
                const hasTotal = Number.isFinite(Number(window.total_paginas_resultados_busqueda_sentencias));
                if (!hasFn || !Number.isFinite(current)) return false;
                const total = hasTotal ? Number(window.total_paginas_resultados_busqueda_sentencias) : null;
                if (total !== null && current >= total - 1) return false;
                window.pagina_resultados_busqueda_sentencias = current + 1;
                window.cargar_datos_resultados_busqueda_sentencias();
                return true;
            }"""
        )
        if advanced_via_js and await _wait_page_change(
            current_page_text,
            current_page_var,
            current_first_id,
            timeout_ms=20_000,
        ):
            log.debug("Avance de página por JS SPA")
            await _wait_results_ready(timeout_ms=15_000)
            return True

        log.warning("Página NO cambió después del avance JS (sigue en %s)", current_page_text)
        return False

    except Exception as e:
        log.warning("Error navegando a siguiente página: %s", e)
        return False


async def paginate_and_collect(
    page: Page,
    db: Database,
    config: Config,
) -> int:
    """
    Recorre las páginas del listado y registra casos en la DB.
    Anti-loop con fingerprint.
    Retorna total de casos nuevos encontrados.
    """
    total_collected = 0
    pages_processed = 0
    prev_fingerprint: str | None = None
    consecutive_same_fp = 0
    current_page_num = config.start_page

    # Navegar a página específica si start_page > 1
    if config.start_page > 1:
        log.info("Navegando a página %d...", config.start_page)
        for _ in range(config.start_page - 1):
            advanced = await _go_to_next_page(page, config)
            if not advanced:
                log.warning("No se pudo avanzar hasta la página %d", config.start_page)
                break
            await asyncio.sleep(config.rate_limit_seconds)

    while True:
        log.info("─── Página %d ───", current_page_num)

        # Extraer casos
        cases = await _extract_cases_from_listing(page)
        if not cases:
            log.info("No se encontraron casos en esta página. Fin de paginación.")
            break

        # ── Anti-loop: fingerprint ─────────────────────────────
        case_ids = [c["case_id"] for c in cases]
        fp = fingerprint_page(case_ids, len(cases))

        if fp == prev_fingerprint:
            consecutive_same_fp += 1
            log.warning(
                "⚠ Fingerprint repetido (%d/2). Posible loop.", consecutive_same_fp
            )
            if consecutive_same_fp >= 2:
                log.error("🛑 Anti-loop: fingerprint repetido 2 veces seguidas. Deteniendo.")
                await _save_debug(page, config.dumps_dir, "antiloop_stop")
                break
        else:
            consecutive_same_fp = 0
        prev_fingerprint = fp

        # ── Registrar en DB ────────────────────────────────────
        for case in cases:
            db.upsert_case(
                case_id=case["case_id"],
                url=case["url"],
                rol=case.get("rol"),
                fecha_sentencia=case.get("fecha_sentencia"),
                caratulado=case.get("caratulado"),
                corte_origen=case.get("corte_origen"),
                page_fingerprint=fp,
            )
            # Guardar idsentencia como setting para poder recuperarla
            db.set_setting(f"idsentencia_{case['case_id']}", case["idsentencia"])
            total_collected += 1

            if config.max_items and total_collected >= config.max_items:
                log.info("Alcanzado max_items=%d", config.max_items)
                return total_collected

        pages_processed += 1
        log.info(
            "Página %d: %d casos (total acumulado: %d)",
            current_page_num, len(cases), total_collected,
        )

        # Límite de páginas
        if config.max_pages and pages_processed >= config.max_pages:
            log.info("Alcanzado max_pages=%d", config.max_pages)
            break

        # Siguiente página
        advanced = await _go_to_next_page(page, config)
        if not advanced:
            log.info("No hay más páginas. Fin.")
            break

        current_page_num += 1
        await asyncio.sleep(config.rate_limit_seconds)

    log.info("Paginación completada: %d casos en %d páginas", total_collected, pages_processed)
    return total_collected
