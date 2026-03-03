"""
Autenticación: detección de sesión, espera de login manual, manejo de expiración.

El login con ClaveÚnica es MANUAL: el script abre el navegador y espera
a que el usuario inicie sesión. No se guardan credenciales.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import Page, BrowserContext

from src.logger import get_logger
from src.selectors import BTN_DESCARGAR_SENTENCIA, LOGGED_IN_INDICATORS, BTN_CLAVE_UNICA

log = get_logger()


async def detect_logged_in(page: Page, timeout_ms: int = 5000) -> bool:
    """
    Verifica si el usuario está autenticado.
    Revisa múltiples indicadores de sesión activa.
    """
    for selector in LOGGED_IN_INDICATORS:
        try:
            el = await page.query_selector(selector)
            if el:
                visible = await el.is_visible()
                if visible:
                    log.debug("Sesión activa detectada con selector: %s", selector)
                    return True
        except Exception:
            continue
    return False


async def detect_download_button(page: Page, timeout_ms: int = 5000) -> bool:
    """
    Verifica específicamente si el botón 'Descargar sentencia original' está presente.
    Esto es el indicador más confiable de sesión activa.
    """
    try:
        el = await page.wait_for_selector(
            BTN_DESCARGAR_SENTENCIA,
            timeout=timeout_ms,
            state="visible",
        )
        return el is not None
    except Exception:
        return False


async def wait_for_manual_login(
    page: Page,
    context: BrowserContext,
    storage_state_path: Path | None = None,
    base_url: str = "https://juris.pjud.cl/busqueda?Corte_Suprema",
) -> bool:
    """
    Guía al usuario para login manual con ClaveÚnica.
    Espera hasta que se detecte sesión activa.
    Retorna True si se logró autenticar.
    """
    log.info("=" * 70)
    log.info("🔑  LOGIN MANUAL REQUERIDO")
    log.info("=" * 70)
    log.info("")
    log.info("  El scraper necesita que inicies sesión con ClaveÚnica.")
    log.info("  Pasos:")
    log.info("    1. En el navegador que se abrió, busca el botón de login / ClaveÚnica")
    log.info("    2. Inicia sesión con tu ClaveÚnica")
    log.info("    3. Espera a que cargue la página de búsqueda")
    log.info("    4. Vuelve aquí y presiona ENTER")
    log.info("")
    log.info("  El script detectará automáticamente cuando estés logueado.")
    log.info("  También puedes simplemente presionar ENTER cuando hayas terminado.")
    log.info("=" * 70)

    # Intentar navegar a la página de login si hay botón
    try:
        login_btn = await page.query_selector(BTN_CLAVE_UNICA)
        if login_btn and await login_btn.is_visible():
            log.info("Botón de login encontrado, haciendo click...")
            await login_btn.click()
            await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception as e:
        log.debug("No se pudo hacer click en botón de login: %s", e)

    # Poll en background mientras esperamos ENTER
    logged_in = False

    async def poll_login() -> None:
        nonlocal logged_in
        while not logged_in:
            await asyncio.sleep(3)
            try:
                logged_in = await detect_logged_in(page, timeout_ms=3000)
                if logged_in:
                    log.info("✅ Sesión detectada automáticamente!")
                    return
            except Exception:
                pass

    poll_task = asyncio.create_task(poll_login())

    # Esperar input del usuario (en thread separado para no bloquear asyncio)
    await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: input("\n>>> Presiona ENTER cuando hayas iniciado sesión... "),
    )

    # Dar un momento para verificar
    if not logged_in:
        await asyncio.sleep(2)
        # Navegar de vuelta a la búsqueda si no estamos ahí
        current = page.url
        if "busqueda" not in current:
            await page.goto(base_url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(2)
        logged_in = await detect_logged_in(page, timeout_ms=10_000)

    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass

    if logged_in:
        log.info("✅ Login confirmado exitosamente")
        # Guardar estado de sesión si se solicita
        if storage_state_path:
            try:
                storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(storage_state_path))
                log.info("Estado de sesión guardado en %s", storage_state_path)
            except Exception as e:
                log.warning("No se pudo guardar estado de sesión: %s", e)
    else:
        log.warning("⚠️  No se detectó sesión activa. Continuando de todas formas...")
        log.warning("   Las descargas de sentencias originales podrían fallar.")

    return logged_in


async def check_session_alive(page: Page) -> bool:
    """
    Verifica si la sesión sigue activa durante el scraping.
    Útil para detectar expiración de sesión.
    """
    return await detect_logged_in(page, timeout_ms=3000)


async def handle_session_expired(
    page: Page,
    context: BrowserContext,
    storage_state_path: Path | None = None,
    base_url: str = "https://juris.pjud.cl/busqueda?Corte_Suprema",
) -> bool:
    """
    Maneja expiración de sesión: pausa y pide re-login.
    """
    log.warning("=" * 70)
    log.warning("⚠️  SESIÓN EXPIRADA")
    log.warning("=" * 70)
    log.warning("  Tu sesión de ClaveÚnica parece haber expirado.")
    log.warning("  Necesitas iniciar sesión nuevamente.")
    log.warning("=" * 70)

    return await wait_for_manual_login(page, context, storage_state_path, base_url)
