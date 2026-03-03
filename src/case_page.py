"""
Procesamiento de la página de detalle de un caso:
- Extraer metadatos del panel izquierdo.
- Navegar tabs (Corte Suprema, Corte Apelaciones, Tribunales).
- Identificar elementos descargables en cada tab.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page

from src.config import Config
from src.db import Database
from src.logger import get_logger
from src.selectors import (
    BTN_DESCARGAR_SENTENCIA,
    BTN_VOLVER_BUSQUEDA,
    DETAIL_ROL_TITLE,
    DOWNLOAD_LINKS,
    META_FIELDS,
    PANEL_DETALLE,
    PANEL_RESULTADOS,
    SECTION_TABS,
)
from src.utils import parse_fecha, sanitize_filename

log = get_logger()


def _speed_factor(config: Config) -> float:
    return 0.4 if config.fast_mode else 1.0


async def _sleep_scaled(config: Config, base_seconds: float, minimum: float = 0.05) -> None:
    await asyncio.sleep(max(minimum, base_seconds * _speed_factor(config)))


async def _save_debug(page: Page, dumps_dir: Path, label: str) -> None:
    """Guarda screenshot + HTML para debug."""
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


async def extract_metadata(page: Page) -> dict:
    """
    Extrae metadatos del panel izquierdo / ficha del caso.
    Usa heurísticas por texto de labels.
    """
    metadata: dict = {
        "rol": None,
        "fecha_sentencia": None,
        "caratulado": None,
        "corte_origen": None,
        "sala": None,
        "materias": None,
        "recurso": None,
        "resultado_recurso": None,
        "raw_fields": {},
    }

    rol_pattern = re.compile(r"\b\d{1,7}-\d{4}\b")

    def norm_label(text: str) -> str:
        txt = (text or "").lower().strip()
        txt = re.sub(r"[^a-z0-9áéíóúñü\s]", " ", txt)
        txt = re.sub(r"\s+", " ", txt)
        return txt.strip()

    def clean_value(text: str | None) -> str | None:
        if not text:
            return None
        value = re.sub(r"\s+", " ", text).strip()
        if not value:
            return None
        if len(value) > 220:
            return None
        if re.match(
            r"(?i)^(rol|caratulado|fecha(?: de sentencia)?|sala|materias?|recurso|resultado recurso|corte de origen|ministro redactor|descriptores|ministros)\s*:?$",
            value,
        ):
            return None
        if re.match(
            r"(?i)^(rol|caratulado|fecha(?: de sentencia)?|sala|materias?|recurso|resultado recurso|corte de origen|ministro redactor|descriptores|ministros)\s*:",
            value,
        ):
            return None
        return value

    def pick_from_raw(
        raw_fields: dict,
        aliases: list[str],
        reject_pattern: str | None = None,
    ) -> str | None:
        wanted = {norm_label(a) for a in aliases}
        fallback: str | None = None
        for raw_label, raw_value in raw_fields.items():
            label_norm = norm_label(raw_label)
            if label_norm in wanted:
                clean = clean_value(raw_value)
                if clean:
                    if reject_pattern and re.match(reject_pattern, clean):
                        if fallback is None:
                            fallback = clean
                        continue
                    return clean
        return fallback

    try:
        # Estrategia 1: buscar pares label-value en la página
        # Muchos sitios judiciales usan <dt>/<dd>, <th>/<td>, o <label>/<span>
        pairs = await page.evaluate(
            """() => {
                const result = [];
                const safeText = (el) => {
                    if (!el) return '';
                    const txt = (typeof el.innerText === 'string' ? el.innerText : el.textContent) || '';
                    return txt.trim();
                };

                // dt/dd pairs
                const dts = document.querySelectorAll('dt');
                dts.forEach(dt => {
                    const dd = dt.nextElementSibling;
                    if (dd && dd.tagName === 'DD') {
                        const label = safeText(dt);
                        const value = safeText(dd);
                        if (label && value) {
                            result.push({label, value});
                        }
                    }
                });

                // th/td pairs (single row tables or horizontal layouts)
                const ths = document.querySelectorAll('th');
                ths.forEach(th => {
                    const td = th.nextElementSibling;
                    if (td && td.tagName === 'TD') {
                        const label = safeText(th);
                        const value = safeText(td);
                        if (label && value) {
                            result.push({label, value});
                        }
                    }
                });

                // label/value with class patterns
                const labels = document.querySelectorAll(
                    '[class*="label"], [class*="field-name"], [class*="key"], ' +
                    '[class*="titulo"], [class*="header"], strong, b'
                );
                labels.forEach(lbl => {
                    const next = lbl.nextElementSibling || lbl.parentElement?.nextElementSibling;
                    if (next) {
                        const label = safeText(lbl);
                        const value = safeText(next);
                        if (label && value) {
                            result.push({label, value});
                        }
                    }
                });

                // Divs with ":" separator
                const divs = document.querySelectorAll('div, span, p, li');
                divs.forEach(div => {
                    const text = safeText(div);
                    if (text.includes(':') && text.length < 300 && div.children.length <= 2) {
                        const parts = text.split(':');
                        if (parts.length === 2) {
                            const label = parts[0].trim();
                            const value = parts[1].trim();
                            if (label && value) {
                                result.push({label, value});
                            }
                        }
                    }
                });

                return result;
            }"""
        )

        # Mapear pares encontrados a campos conocidos
        for pair in pairs:
            label = (pair.get("label") or "").strip()
            value = (pair.get("value") or "").strip()
            label_lower = label.lower()

            if not label or not value or len(value) > 500:
                continue

            metadata["raw_fields"][label] = value

            for field_name, patterns in META_FIELDS.items():
                if metadata[field_name]:
                    continue
                for pattern in patterns:
                    if pattern.lower() in label_lower or label_lower in pattern.lower():
                        if field_name == "fecha_sentencia":
                            metadata[field_name] = parse_fecha(value)
                        else:
                            metadata[field_name] = value
                        break

        # Estrategia 2: buscar ROL desde título principal del detalle
        rol_from_detail_title = None
        try:
            rol_title_text = await page.inner_text(DETAIL_ROL_TITLE)
            match = rol_pattern.search(rol_title_text or "")
            if match:
                rol_from_detail_title = match.group(0)
                metadata["rol"] = rol_from_detail_title
        except Exception:
            pass

        # Estrategia 3: buscar ROL en el título de la página o header
        if not metadata["rol"]:
            try:
                title = await page.title()
                rol_match = re.search(r"\b(\d{1,7}-\d{4})\b", title)
                if rol_match:
                    metadata["rol"] = rol_match.group(1)
            except Exception:
                pass

            # Buscar en h1, h2, h3
            for tag in ["h1", "h2", "h3"]:
                try:
                    headers = await page.query_selector_all(tag)
                    for h in headers:
                        text = await h.inner_text()
                        rol_match = re.search(r"\b(\d{1,7}-\d{4})\b", text)
                        if rol_match:
                            metadata["rol"] = rol_match.group(1)
                            break
                except Exception:
                    continue
                if metadata["rol"]:
                    break

        # Normalización final: ROL debe tener formato NNNNN-NNNN
        raw_rol = metadata.get("rol") or ""
        rol_match = rol_pattern.search(raw_rol)
        if rol_match:
            metadata["rol"] = rol_match.group(0)
        else:
            metadata["rol"] = None
            # Recuperar desde campos crudos
            for raw_label, raw_value in metadata["raw_fields"].items():
                probe = f"{raw_label} {raw_value}"
                match = rol_pattern.search(probe)
                if match:
                    metadata["rol"] = match.group(0)
                    break

            # Último fallback: texto completo visible del panel
            if not metadata["rol"]:
                try:
                    panel_text = await page.inner_text(PANEL_DETALLE)
                    match = re.search(
                        r"(?i)\brol\b[^0-9]{0,20}(\d{1,7}-\d{4})\b",
                        panel_text,
                    )
                    if match:
                        metadata["rol"] = match.group(1)
                except Exception:
                    pass

        # Priorización de labels exactos del panel para evitar cruces de campos
        raw_fields = metadata.get("raw_fields", {})

        rol_from_raw = pick_from_raw(raw_fields, ["ROL", "Rol"])
        if rol_from_raw and not rol_from_detail_title:
            exact = rol_pattern.search(rol_from_raw)
            if exact:
                metadata["rol"] = exact.group(0)

        fecha_from_raw = pick_from_raw(
            raw_fields,
            ["Fecha de sentencia", "Fecha sentencia", "Fecha Sentencia"],
        )
        if fecha_from_raw:
            parsed = parse_fecha(fecha_from_raw)
            if parsed:
                metadata["fecha_sentencia"] = parsed

        caratulado_from_raw = pick_from_raw(
            raw_fields,
            ["Caratulado", "Carátula"],
            reject_pattern=r"(?i)^(fecha|sala|materias|recurso|resultado recurso|corte de origen|ministro redactor)\b",
        )
        if caratulado_from_raw:
            metadata["caratulado"] = caratulado_from_raw

        sala_from_raw = pick_from_raw(raw_fields, ["Sala"])
        if sala_from_raw and "seleccionar todo" not in sala_from_raw.lower():
            metadata["sala"] = sala_from_raw
        else:
            metadata["sala"] = None

        corte_from_raw = pick_from_raw(raw_fields, ["Corte de origen", "Corte origen"])
        if corte_from_raw and "seleccionar todo" not in corte_from_raw.lower():
            metadata["corte_origen"] = corte_from_raw
        else:
            metadata["corte_origen"] = None

        materias_from_raw = pick_from_raw(raw_fields, ["Materias", "Materia"])
        if materias_from_raw and not re.match(
            r"(?i)^(recurso|resultado recurso|corte de origen)\s*:",
            materias_from_raw,
        ):
            metadata["materias"] = materias_from_raw
        else:
            metadata["materias"] = None

        recurso_from_raw = pick_from_raw(raw_fields, ["Recurso", "Tipo recurso"])
        if recurso_from_raw and not re.match(
            r"(?i)^(resultado recurso|corte de origen|ministro redactor)\s*:",
            recurso_from_raw,
        ):
            metadata["recurso"] = recurso_from_raw
        else:
            metadata["recurso"] = None

        resultado_from_raw = pick_from_raw(
            raw_fields,
            ["Resultado recurso", "Resultado del recurso"],
        )
        if resultado_from_raw and not re.match(
            r"(?i)^(corte de origen|ministro redactor)\s*:",
            resultado_from_raw,
        ):
            metadata["resultado_recurso"] = resultado_from_raw
        else:
            metadata["resultado_recurso"] = None

        # Fallback práctico: si Materias viene vacío, usar Recurso para clasificar.
        if not metadata.get("materias") and metadata.get("recurso"):
            metadata["materias"] = metadata["recurso"]

    except Exception as e:
        log.warning("Error extrayendo metadatos: %s", e)

    return metadata


async def extract_sentence_text(page: Page) -> str | None:
    """
    Extrae texto de la sentencia desde el detalle inline.
    Usa varias heurísticas de selectores y se queda con el bloque más probable.
    """
    try:
        # Estrategia 0 (preferida): panel central del detalle, donde está el cuerpo.
        direct_text = None
        try:
            direct_text = await page.inner_text("#panel_contenedor_central_detalle_sentencia")
        except Exception:
            direct_text = None

        if direct_text and len(direct_text.strip()) >= 120:
            lines = [ln.strip() for ln in re.sub(r"\r\n?", "\n", direct_text).split("\n")]
            drop_exact = {
                "Corte Suprema",
                "Corte Apelaciones",
                "Tribunales",
                "Navegar en la sentencia",
                "Destacar información",
            }
            cleaned_lines = []
            for ln in lines:
                if not ln:
                    cleaned_lines.append("")
                    continue
                if ln in drop_exact:
                    continue
                if ln.startswith("Sentencia Fecha Sentencia Cuerpo Sentencia"):
                    continue
                if ln.startswith("Marcar todas"):
                    continue
                cleaned_lines.append(ln)
            cleaned_direct = "\n".join(cleaned_lines)
            cleaned_direct = re.sub(r"\n{3,}", "\n\n", cleaned_direct).strip()
            if len(cleaned_direct) >= 120:
                return cleaned_direct

        text = await page.evaluate(
            f"""() => {{
                const root = document.querySelector('{PANEL_DETALLE}') || document;
                const candidates = [];

                const selectors = [
                    "#nav-tabTexto_sentenciaContent",
                    "[id^='capa_notas_texto_sentencia']",
                    "[id^='capa_texto_sentencia']",
                    "#panel_contenedor_central_detalle_sentencia",
                    ".tab-pane.active",
                    "[role='tabpanel'].active",
                    "[aria-labelledby*='cuerpo']",
                    "[id*='cuerpo_sentencia']",
                    "[id*='contenido_sentencia']",
                    "[id*='texto_sentencia']",
                    "[class*='cuerpo_sentencia']",
                    "[class*='contenido_sentencia']",
                    "[class*='texto_sentencia']",
                    "[class*='sentencia']",
                    "article",
                    "section",
                    "div",
                ];

                const seen = new Set();
                for (const sel of selectors) {{
                    const nodes = root.querySelectorAll(sel);
                    for (const n of nodes) {{
                        if (n.closest('.modal')) continue;
                        if ((n.id || '').toLowerCase().includes('modal')) continue;
                        const txt = (n.innerText || n.textContent || "").trim();
                        if (!txt || txt.length < 300) continue;
                        const low = txt.toLowerCase();
                        if (
                            low.includes('leyenda: en contra')
                        ) continue;
                        if (seen.has(txt)) continue;
                        seen.add(txt);
                        candidates.push(txt);
                    }}
                }}

                const score = (txt) => {{
                    const t = txt.toLowerCase();
                    let s = 0;
                    if (t.includes("navegar en la sentencia")) s -= 5;
                    if (t.includes("destacar información")) s -= 5;
                    if (t.includes("recopilación de términos frecuentes")) s -= 5;
                    if (t.includes("datos de la sentencia")) s -= 4;
                    if (t.includes("vistos")) s += 3;
                    if (t.includes("considerando")) s += 3;
                    if (t.includes("por estas consideraciones")) s += 2;
                    if (t.includes("resuelvo") || t.includes("se resuelve")) s += 2;
                    if (t.includes("regístrese")) s += 1;
                    return s * 10000 + txt.length;
                }};

                if (!candidates.length) return null;
                candidates.sort((a, b) => score(b) - score(a));
                return candidates[0];
            }}"""
        )

        if not text:
            return None

        # Limpieza básica de texto UI/no jurídico
        cleaned = re.sub(r"\r\n?", "\n", text)
        junk_patterns = [
            r"(?im)^navegar en la sentencia.*$",
            r"(?im)^destacar información.*$",
            r"(?im)^guardar sentencia.*$",
            r"(?im)^compartir sentencia.*$",
            r"(?im)^recopilación de términos frecuentes.*$",
            r"(?im)^sugerencia de búsqueda.*$",
            r"(?im)^buscar\s*$",
            r"(?im)^limpiar\s*$",
        ]
        for p in junk_patterns:
            cleaned = re.sub(p, "", cleaned)

        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        lower_clean = cleaned.lower()

        # Si solo trae encabezado UI, intentamos un último rescate del panel central.
        if "datos de la sentencia" in lower_clean and not (
            "considerando" in lower_clean or "vistos" in lower_clean
        ):
            try:
                rescue = await page.inner_text("#panel_contenedor_central_detalle_sentencia")
                rescue = re.sub(r"\r\n?", "\n", rescue or "").strip()
                if len(rescue) >= 120:
                    return rescue
            except Exception:
                pass

        return cleaned if len(cleaned) >= 120 else None
    except Exception as e:
        log.debug("No se pudo extraer texto de sentencia: %s", e)
        # Último fallback fuerte: intentar sacar lo que haya en el panel central.
        try:
            rescue = await page.inner_text("#panel_contenedor_central_detalle_sentencia")
            rescue = re.sub(r"\r\n?", "\n", rescue or "").strip()
            return rescue if len(rescue) >= 120 else None
        except Exception:
            return None


async def _activate_sentence_content_tab(page: Page, config: Config) -> None:
    """
    Intenta activar la pestaña donde está el cuerpo de la sentencia
    para extraer texto jurídico y no solo texto de interfaz.
    """
    candidates = [
        "a:has-text('Cuerpo Sentencia')",
        "button:has-text('Cuerpo Sentencia')",
        "a:has-text('Sentencia')",
        "button:has-text('Sentencia')",
    ]
    for sel in candidates:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await _sleep_scaled(config, 0.6)
                return
        except Exception:
            continue


async def _click_tab(page: Page, section: str, config: Config) -> bool:
    """
    Hace click en la pestaña de una sección.
    Retorna True si se clickeó exitosamente.
    """
    selector = SECTION_TABS.get(section)
    if not selector:
        log.warning("Sección desconocida: %s", section)
        return False

    async def _dismiss_blocking_modals() -> None:
        await page.evaluate(
            """() => {
                const modals = document.querySelectorAll('.modal.show');
                modals.forEach(m => {
                    const closeBtn =
                        m.querySelector('[data-dismiss="modal"]') ||
                        m.querySelector('.close') ||
                        m.querySelector('button.btn-close') ||
                        m.querySelector('button:has-text("Cerrar")');
                    if (closeBtn && typeof closeBtn.click === 'function') {
                        closeBtn.click();
                    } else {
                        m.classList.remove('show');
                        m.style.display = 'none';
                        m.setAttribute('aria-hidden', 'true');
                    }
                });
                const backdrops = document.querySelectorAll('.modal-backdrop');
                backdrops.forEach(b => b.remove());
                document.body.classList.remove('modal-open');
                document.body.style.removeProperty('padding-right');
            }"""
        )

    try:
        for attempt in range(1, 4):
            await _dismiss_blocking_modals()
            await _sleep_scaled(config, 0.2)

            tab = await page.query_selector(selector)
            if not tab:
                log.debug("Tab '%s' no encontrado", section)
                return False

            is_visible = await tab.is_visible()
            if not is_visible:
                log.debug("Tab '%s' no visible", section)
                return False

            clicked = False
            try:
                await tab.click(timeout=1500)
                clicked = True
            except Exception:
                clicked = await page.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return false;
                        try { el.click(); return true; } catch (_) { return false; }
                    }""",
                    selector,
                )

            if not clicked:
                continue

            # Espera corta, no bloqueante por networkidle (el sitio mantiene requests)
            await _sleep_scaled(config, 0.7)

            # Validar estado activo del tab
            is_active = await page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    return el.classList.contains('active') || el.getAttribute('aria-selected') === 'true';
                }""",
                selector,
            )
            if is_active:
                log.debug("Tab '%s' clickeado", section)
                return True

            log.debug("Tab '%s' intento %d sin activar", section, attempt)

        return False

    except Exception as e:
        log.debug("Error clickeando tab '%s': %s", section, e)
        return False


async def find_downloadables(page: Page, section: str) -> list[dict]:
    """
    Dentro del panel activo de una sección, encuentra elementos descargables.
    Retorna lista de dicts con info de cada elemento.
    """
    items: list[dict] = []

    # ── 1. Botón "Descargar sentencia original" ────────────────
    try:
        btn = await page.query_selector(BTN_DESCARGAR_SENTENCIA)
        if btn and await btn.is_visible():
            items.append({
                "label": "sentencia_original",
                "element": btn,
                "source": "button",
                "url": None,
            })
            log.debug("[%s] Encontrado: Descargar sentencia original", section)
    except Exception as e:
        log.debug("[%s] Error buscando botón de descarga: %s", section, e)

    # ── 2. Links de descarga (PDFs, docs, etc.) ────────────────
    try:
        links = await page.query_selector_all(DOWNLOAD_LINKS)
        seen_hrefs: set = set()
        idx = 1

        for link in links:
            try:
                # Verificar visibilidad
                if not await link.is_visible():
                    continue

                href = await link.get_attribute("href")
                text = (await link.inner_text()).strip()

                # Evitar duplicados
                link_key = href or text
                if link_key in seen_hrefs:
                    continue
                seen_hrefs.add(link_key)

                # Evitar el botón de sentencia original (ya capturado)
                if "sentencia original" in text.lower():
                    continue

                # Evitar links de navegación
                if text.lower() in ("ver sentencia", "volver", "atrás", "cerrar"):
                    continue

                label = sanitize_filename(text) if text else f"anexo_{idx:03d}"
                # Si el label es genérico, intentar usar el filename del href
                if label.lower() in ("descargar", "download", "unnamed"):
                    if href:
                        fname = href.rsplit("/", 1)[-1].split("?")[0]
                        if fname:
                            label = sanitize_filename(fname)

                items.append({
                    "label": label,
                    "element": link,
                    "source": "link",
                    "url": href,
                })
                log.debug("[%s] Encontrado link: %s → %s", section, label, href or "(no href)")
                idx += 1

            except Exception:
                continue

    except Exception as e:
        log.debug("[%s] Error buscando links de descarga: %s", section, e)

    return items


async def _open_case_detail(page: Page, idsentencia: str, config: Config) -> bool:
    """
    Abre el detalle de un caso clickeando el span[data-idsentencia] en el listado.
    El sitio es SPA: el detalle se carga inline via JS sin cambiar URL.
    Retorna True si se abrió exitosamente.
    """
    selector = f'{PANEL_RESULTADOS} [data-idsentencia="{idsentencia}"]'

    async def _wait_detail_loaded(timeout_ms: int) -> bool:
        deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000)
        rol_re = re.compile(r"\d{1,7}-\d{4}")
        while asyncio.get_event_loop().time() < deadline:
            try:
                detail = await page.query_selector(PANEL_DETALLE)
                if detail and await detail.is_visible():
                    rol_text = ""
                    try:
                        rol_text = (await page.inner_text(DETAIL_ROL_TITLE)).strip()
                    except Exception:
                        rol_text = ""
                    if rol_re.search(rol_text):
                        return True
            except Exception:
                pass
            await _sleep_scaled(config, 0.4)
        return False

    try:
        # Estrategia preferida: llamada directa del loader SPA por idsentencia.
        opened_by_loader = await page.evaluate(
            """(sid) => {
                if (typeof window.cargar_detalle_sentencia !== "function") return false;
                try {
                    window.cargar_detalle_sentencia(String(sid));
                    if (window.$) {
                        $('#capa_contenedor_detalle_sentencia').show();
                        $('#capa_contenedor_controles_busqueda').hide();
                        $('#contenedor_estadisticas_visitas_y_busquedas').hide();
                    }
                    return true;
                } catch (_) {
                    return false;
                }
            }""",
            idsentencia,
        )
        if opened_by_loader:
            if await _wait_detail_loaded(min(config.timeout_ms, 20_000)):
                return True

        el = await page.query_selector(selector)
        if not el:
            # Fallback: intentar buscar por JS onclick ver_detalle_sentencia
            el = await page.query_selector(
                f'[onclick*="ver_detalle_sentencia"][onclick*="{idsentencia}"]'
            )
        if el and await el.is_visible():
            await el.click()
        else:
            log.warning("No se encontró el caso %s en la página actual", idsentencia)
            # Fallback 2: click via JS sobre cualquier nodo con data-idsentencia
            clicked = await page.evaluate(
                """(sid) => {
                    const n = document.querySelector(`[data-idsentencia="${sid}"]`);
                    if (!n) return false;
                    try { n.click(); return true; } catch(_) { return false; }
                }""",
                idsentencia,
            )
            if clicked:
                await _sleep_scaled(config, 1.0)
            else:
                # Fallback 3 robusto: invocar JS SPA directamente por idsentencia
                invoked = await page.evaluate(
                    """(sid) => {
                        const fn = window.ver_detalle_sentencia;
                        if (typeof fn !== "function") return false;
                        const fakeBtn = { dataset: { idsentencia: String(sid) } };
                        try { fn(fakeBtn); return true; } catch (_) {}
                        try { fn({ dataset: { idsentencia: sid } }); return true; } catch (_) {}
                        return false;
                    }""",
                    idsentencia,
                )
                if not invoked:
                    return False

        await _sleep_scaled(config, 1.0)

        # Esperar a que aparezca el panel de detalle
        try:
            await page.wait_for_selector(
                PANEL_DETALLE, state="visible", timeout=config.timeout_ms
            )
        except Exception:
            # Fallback: esperar un poco más
            await _sleep_scaled(config, 3.0, minimum=0.2)

        if not config.fast_mode:
            await page.wait_for_load_state("networkidle", timeout=config.timeout_ms // 2)
        await _sleep_scaled(config, 1.0)

        # Verificar que el panel de detalle está visible
        if await _wait_detail_loaded(min(config.timeout_ms, 20_000)):
            return True

        # Último fallback: invocar función SPA manualmente
        invoked = await page.evaluate(
            """(sid) => {
                const fn = window.ver_detalle_sentencia;
                if (typeof fn !== "function") return false;
                try { fn({ dataset: { idsentencia: String(sid) } }); return true; } catch (_) {}
                return false;
            }""",
            idsentencia,
        )
        if invoked:
            if await _wait_detail_loaded(min(config.timeout_ms, 20_000)):
                return True

        log.warning("Panel de detalle no visible tras click en caso %s", idsentencia)
        return False

    except Exception as e:
        log.warning("Error abriendo detalle del caso %s: %s", idsentencia, e)
        return False


async def _close_case_detail(page: Page, config: Config) -> bool:
    """
    Cierra el detalle de un caso clickeando "Volver a la página de búsqueda".
    """
    try:
        async def _dismiss_blocking_modals() -> None:
            await page.evaluate(
                """() => {
                    const modals = document.querySelectorAll('.modal.show');
                    modals.forEach(m => {
                        const closeBtn =
                            m.querySelector('[data-dismiss="modal"]') ||
                            m.querySelector('.close') ||
                            m.querySelector('button.btn-close');
                        if (closeBtn && typeof closeBtn.click === 'function') {
                            closeBtn.click();
                        } else {
                            m.classList.remove('show');
                            m.style.display = 'none';
                            m.setAttribute('aria-hidden', 'true');
                        }
                    });
                    const backdrops = document.querySelectorAll('.modal-backdrop');
                    backdrops.forEach(b => b.remove());
                    document.body.classList.remove('modal-open');
                    document.body.style.removeProperty('padding-right');
                }"""
            )

        async def _click_with_fallback(sel: str) -> bool:
            btn = await page.query_selector(sel)
            if not btn or not await btn.is_visible():
                return False
            await _dismiss_blocking_modals()
            try:
                await btn.click(timeout=1500)
                return True
            except Exception:
                return await page.evaluate(
                    """(selector) => {
                        const el = document.querySelector(selector);
                        if (!el) return false;
                        try { el.click(); return true; } catch (_) { return false; }
                    }""",
                    sel,
                )

        btn = await page.query_selector(BTN_VOLVER_BUSQUEDA)
        if btn and await btn.is_visible():
            if await _click_with_fallback(BTN_VOLVER_BUSQUEDA):
                await _sleep_scaled(config, 0.8)
                return True

        # Fallback: buscar cualquier botón de volver
        for sel in [
            "button:has-text('Volver')",
            "a:has-text('Volver')",
            "button:has-text('volver')",
            "#btn_volver_busqueda",
        ]:
            if await _click_with_fallback(sel):
                await _sleep_scaled(config, 0.8)
                return True

        # Último recurso: recargar la página de búsqueda
        log.warning("No se encontró botón volver, recargando búsqueda")
        await page.goto(config.base_url, wait_until="networkidle", timeout=config.timeout_ms)
        await _sleep_scaled(config, 2.0, minimum=0.2)
        return True

    except Exception as e:
        log.warning("Error cerrando detalle: %s", e)
        return False


async def process_case_page(
    page: Page,
    case_id: str,
    case_url: str,
    db: Database,
    config: Config,
    idsentencia: str | None = None,
) -> dict:
    """
    Procesa una página de caso completa:
    1. Si hay idsentencia, abre el detalle inline (SPA)
    2. Si no, navega a la URL del caso como fallback
    3. Extrae metadatos
    4. Retorna info de secciones y descargables
    """
    log.info("Procesando caso %s: %s", case_id[:12], case_url)

    # ── Abrir el detalle del caso ──────────────────────────────
    opened_inline = False
    if idsentencia:
        opened_inline = await _open_case_detail(page, idsentencia, config)
        if opened_inline:
            log.debug("Detalle abierto inline para idsentencia=%s", idsentencia)

    if not opened_inline:
        # Fallback: navegar a la URL (puede funcionar si el hash trigger JS)
        log.debug("Usando fallback: page.goto(%s)", case_url)
        try:
            await page.goto(case_url, wait_until="networkidle", timeout=config.timeout_ms)
            await _sleep_scaled(config, 2.0, minimum=0.2)
            if idsentencia:
                await _open_case_detail(page, idsentencia, config)
            # Esperar a que cargue el detalle por el fragment
            try:
                await page.wait_for_selector(
                    PANEL_DETALLE, state="visible", timeout=10_000
                )
            except Exception:
                pass
        except Exception as e:
            log.error("Error navegando a caso %s: %s", case_id[:12], e)
            raise

    # Extraer metadatos
    metadata = await extract_metadata(page)
    await _activate_sentence_content_tab(page, config)
    sentencia_text = await extract_sentence_text(page)
    log.info(
        "Metadatos: rol=%s, fecha=%s, caratulado=%s",
        metadata.get("rol"),
        metadata.get("fecha_sentencia"),
        metadata.get("caratulado", "")[:60],
    )
    if sentencia_text:
        log.info("Texto sentencia extraído (%d chars)", len(sentencia_text))
    else:
        log.info("Texto sentencia no disponible")

    # Actualizar caso en DB con metadatos más completos
    db.upsert_case(
        case_id=case_id,
        url=case_url,
        rol=metadata.get("rol"),
        fecha_sentencia=metadata.get("fecha_sentencia"),
        caratulado=metadata.get("caratulado"),
        corte_origen=metadata.get("corte_origen"),
        sala=metadata.get("sala"),
        materias=metadata.get("materias"),
        recurso=metadata.get("recurso"),
        resultado_recurso=metadata.get("resultado_recurso"),
    )

    # Recopilar descargables y textos por sección
    sections_data: dict[str, list[dict]] = {}
    section_texts: dict[str, str] = {}

    section_name_map = {
        "SUPREMA": "Corte Suprema",
        "APELACIONES": "Corte Apelaciones",
        "TRIBUNALES": "Tribunales",
    }
    for section in config.sections_enabled():
        log.info("── Tab: %s ──", section_name_map.get(section, section))

        # Intentar click en tab
        tab_clicked = await _click_tab(page, section, config)
        if not tab_clicked:
            log.info("Tab %s no disponible, buscando en contenido actual", section)
        else:
            # Dentro de cada instancia, ir al sub-tab de texto y extraer.
            await _activate_sentence_content_tab(page, config)
            txt = await extract_sentence_text(page)
            if txt:
                section_texts[section] = txt
                log.info("  Texto %s: %d chars", section, len(txt))

        # Encontrar descargables
        downloadables = await find_downloadables(page, section)
        sections_data[section] = downloadables

        if downloadables:
            log.info("  → %d elemento(s) descargable(s)", len(downloadables))
        else:
            log.info("  → Sin elementos descargables")

    # Fallback: si no logramos etiqueta SUPREMA por tab, usar el texto principal.
    if "SUPREMA" not in section_texts and sentencia_text:
        section_texts["SUPREMA"] = sentencia_text

    primary_text = (
        section_texts.get("SUPREMA")
        or next(iter(section_texts.values()), None)
        or sentencia_text
    )

    # Guardar metadatos JSON
    result = {
        "case_id": case_id,
        "url": case_url,
        "metadata": {
            "rol": metadata.get("rol"),
            "fecha_sentencia": metadata.get("fecha_sentencia"),
            "caratulado": metadata.get("caratulado"),
            "corte_origen": metadata.get("corte_origen"),
            "sala": metadata.get("sala"),
            "materias": metadata.get("materias"),
            "recurso": metadata.get("recurso"),
            "resultado_recurso": metadata.get("resultado_recurso"),
            "raw_fields": metadata.get("raw_fields", {}),
        },
        "sections": {
            s: [{"label": d["label"], "source": d["source"], "url": d.get("url")}
                for d in dls]
            for s, dls in sections_data.items()
        },
        "scraped_at": datetime.now().isoformat(),
        "sentencia_text": primary_text,
        "sentencias_por_seccion": section_texts,
    }

    return {**result, "_downloadables": sections_data}


def save_case_meta(
    case_data: dict,
    output_dir: Path,
    fecha: str | None,
    sala: str | None,
) -> Path:
    """
    Guarda metadatos del caso como JSON en out/meta/YYYY/MM/<case_id>.json.
    """
    from src.utils import fecha_to_ym
    from src.filesystem import get_meta_dir

    year, _ = fecha_to_ym(fecha)
    meta_dir = get_meta_dir(output_dir, sala, year)
    meta_dir.mkdir(parents=True, exist_ok=True)

    # Remover _downloadables (contiene elementos Playwright no serializables)
    clean_data = {k: v for k, v in case_data.items() if not k.startswith("_")}

    filepath = meta_dir / f"{case_data['case_id']}.json"
    filepath.write_text(json.dumps(clean_data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.debug("Meta guardado: %s", filepath)
    return filepath


def save_case_text(
    case_data: dict,
    output_dir: Path,
    fecha: str | None,
    sala: str | None,
) -> Path | None:
    """
    Guarda un archivo unificado por causa en out/sentencias/<sala>/<year>/<case_id>.txt.
    El archivo incluye metadata y luego textos por instancia.
    """
    from src.utils import fecha_to_ym
    from src.filesystem import get_text_dir

    section_texts = case_data.get("sentencias_por_seccion") or {}
    if not section_texts:
        fallback = (case_data.get("sentencia_text") or "").strip()
        if fallback:
            section_texts = {"SUPREMA": fallback}

    if not section_texts:
        return None

    year, _ = fecha_to_ym(fecha)
    text_dir = get_text_dir(output_dir, sala, year)
    text_dir.mkdir(parents=True, exist_ok=True)

    metadata = case_data.get("metadata", {}) or {}
    meta_header = [
        "METADATA",
        f"case_id: {case_data.get('case_id', '')}",
        f"url: {case_data.get('url', '')}",
        f"rol: {metadata.get('rol') or ''}",
        f"fecha_sentencia: {metadata.get('fecha_sentencia') or ''}",
        f"caratulado: {metadata.get('caratulado') or ''}",
        f"sala: {metadata.get('sala') or ''}",
        f"materias: {metadata.get('materias') or ''}",
        f"recurso: {metadata.get('recurso') or ''}",
        f"resultado_recurso: {metadata.get('resultado_recurso') or ''}",
    ]

    section_order = ["SUPREMA", "APELACIONES", "TRIBUNALES"]
    ordered_sections = [s for s in section_order if s in section_texts] + [
        s for s in section_texts.keys() if s not in section_order
    ]

    section_blocks: list[str] = []
    for section in ordered_sections:
        txt = (section_texts.get(section) or "").strip()
        if not txt:
            continue
        section_blocks.append(
            "\n".join(
                [
                    f"TEXTO_SENTENCIA_{section}",
                    "-" * 80,
                    txt,
                ]
            )
        )

    full_text = "\n\n".join(meta_header + section_blocks).strip() + "\n"

    filepath = text_dir / f"{case_data['case_id']}.txt"
    filepath.write_text(full_text, encoding="utf-8")
    log.debug("Texto sentencia guardado: %s", filepath)
    return filepath
