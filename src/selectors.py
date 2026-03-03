"""
Selectores CSS y heurísticas para juris.pjud.cl.

Basados en la estructura real del DOM (jQuery + Bootstrap, NO Angular).
El sitio usa IDs y clases estables; el detalle se carga inline via JS
(ver_detalle_sentencia) sin cambiar URL.
"""

from __future__ import annotations

# ══════════════════════════════════════════════════════════════════
#  PÁGINA DE LISTADO (búsqueda)
# ══════════════════════════════════════════════════════════════════

# Cada resultado es un <span> clickeable con data-idsentencia
RESULT_ITEM = "span.estilo_resultado_titulo[data-idsentencia]"

# Card contenedora de cada resultado
RESULT_CARD_HEADER = ".card-header"

# Select nativo de resultados por página (id fijo)
RESULTS_PER_PAGE_SELECT_ID = "resultados_busqueda_registros_por_pagina"

# Paginación: <ul class="pagination"> con <li class="page-item"><a class="page-link">
PAGINATION_UL = "ul.pagination"
PAGINATION_NEXT = "ul.pagination li:nth-last-child(2) a.page-link"  # ▶ (penúltimo = siguiente)
PAGINATION_LAST = "ul.pagination li:last-child a.page-link"          # ▶▶
PAGINATION_ACTIVE = "ul.pagination li.page-item.active a.page-link"

# Panel de resultados vs panel de detalle
PANEL_RESULTADOS = "#panel_central_busqueda"
PANEL_DETALLE = "#capa_contenedor_detalle_sentencia"

# Botón "Volver a la página de búsqueda" dentro del detalle
BTN_VOLVER_BUSQUEDA = (
    "#capa_contenedor_detalle_sentencia "
    "button.btn.btn-dark"
)

# ══════════════════════════════════════════════════════════════════
#  PÁGINA DE DETALLE (inline)
# ══════════════════════════════════════════════════════════════════

# Título con ROL
DETAIL_ROL_TITLE = "#span_rol_sentencia_titulo_superior"

# ── Tabs de instancia ──────────────────────────────────────────
TAB_CORTE_SUPREMA = "#nav-corte_suprema-tab"
TAB_CORTE_APELACIONES = "#nav-corte_apelaciones-tab"
TAB_TRIBUNALES = "#nav-tribunales-tab"

SECTION_TABS = {
    "SUPREMA": TAB_CORTE_SUPREMA,
    "APELACIONES": TAB_CORTE_APELACIONES,
    "TRIBUNALES": TAB_TRIBUNALES,
}

# ── Tab panels (contenido de cada instancia) ──────────────────
TAB_PANEL_SUPREMA = "#nav-corte_suprema"
TAB_PANEL_APELACIONES = "#nav-corte_apelaciones"
TAB_PANEL_TRIBUNALES = "#nav-tribunales"

SECTION_PANELS = {
    "SUPREMA": TAB_PANEL_SUPREMA,
    "APELACIONES": TAB_PANEL_APELACIONES,
    "TRIBUNALES": TAB_PANEL_TRIBUNALES,
}

# Texto que indica que NO hay documento en la instancia
NO_DOCUMENT_TEXT = "Sin documento en esta instancia"

# ── Botón descarga sentencia original (requiere ClaveÚnica) ───
BTN_DESCARGAR_SENTENCIA = (
    "button:has-text('Descargar sentencia original'), "
    "a:has-text('Descargar sentencia original'), "
    "button:has-text('Descargar Sentencia Original'), "
    "a:has-text('Descargar Sentencia Original'), "
    "button:has-text('Descargar documento original'), "
    "a:has-text('Descargar documento original'), "
    "[onclick*='descargar_sentencia'], "
    "[onclick*='descargar_documento']"
)

# Links genéricos de descarga dentro de un tab panel
DOWNLOAD_LINKS = (
    "a[href$='.pdf'], "
    "a[href$='.doc'], "
    "a[href$='.docx'], "
    "a[href$='.zip'], "
    "a[href*='download'], "
    "a[href*='descargar'], "
    "a:has-text('Descargar'), "
    "button:has-text('Descargar')"
)

# ── Metadatos del panel izquierdo ─────────────────────────────
# Los metadatos están como <div><b>Label:</b> valor</div>
# dentro de #capa_contenedor_detalle_sentencia
META_CONTAINER = "#capa_contenedor_detalle_sentencia"
CITA_BIBLIOGRAFICA = "#capa_contenido_cita_bibliografica"

META_FIELDS = {
    "rol": ["ROL", "Rol"],
    "fecha_sentencia": ["Fecha de sentencia", "Fecha sentencia", "Fecha Sentencia",
                        "Fecha resolución", "Fecha de la sentencia"],
    "caratulado": ["Caratulado", "Carátula", "Partes"],
    "corte_origen": ["Corte de origen", "Corte origen", "Corte"],
    "sala": ["Sala"],
    "materias": ["Materias", "Materia"],
    "recurso": ["Recurso"],
    "resultado_recurso": ["Resultado recurso", "Resultado del recurso"],
}

# ══════════════════════════════════════════════════════════════════
#  AUTENTICACIÓN
# ══════════════════════════════════════════════════════════════════

# El sitio muestra "Acceder a mi cuenta" cuando NO está logueado
LINK_ACCEDER_CUENTA = 'a.nav-link:has-text("Acceder a mi cuenta")'

# Indicadores de sesión activa (aparecen SOLO al estar logueado)
LOGGED_IN_INDICATORS = [
    BTN_DESCARGAR_SENTENCIA,
    "a:has-text('Cerrar sesión')",
    "a:has-text('Mi cuenta')",
    "[class*='user-logged']",
    "a:has-text('Salir')",
]

# Botón/link de login con ClaveÚnica
BTN_CLAVE_UNICA = (
    "a:has-text('ClaveÚnica'), "
    "a:has-text('Clave Única'), "
    "a:has-text('Acceder a mi cuenta'), "
    "a:has-text('Iniciar sesión')"
)
