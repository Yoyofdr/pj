# pjud-scraper

Descargador automatizado de sentencias desde el Poder Judicial de Chile ([juris.pjud.cl](https://juris.pjud.cl/busqueda?Corte_Suprema)).

Descarga documentos de las secciones **Corte Suprema**, **Corte de Apelaciones** y **Tribunales**, organizándolos por año/mes. El login con ClaveÚnica es **manual** — el script abre un navegador y espera a que inicies sesión.

---

## Quick Start

### 1. Instalar dependencias

```bash
# Crear entorno virtual (recomendado)
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# Instalar paquetes
pip install -r requirements.txt

# Instalar navegadores de Playwright
playwright install chromium
```

### 2. Ejecutar (modo test)

```bash
python -m src.main --test-mode
```

Esto procesará solo 1 página con máximo 5 casos — ideal para verificar que todo funciona.

### 3. Ejecutar (completo)

```bash
python -m src.main
```

### 4. Login manual

Al ejecutar, el script:
1. Abre un navegador Chromium visible (headful).
2. Navega a juris.pjud.cl.
3. Si no detecta sesión activa, imprime instrucciones en consola.
4. **Tú** haces login con ClaveÚnica en el navegador abierto.
5. Vuelves a la terminal y presionas **ENTER**.
6. El script continúa automáticamente.

La sesión se persiste en `./state/storage.json` entre ejecuciones (configurable con `--persist-session`).

---

## Flags CLI

| Flag | Default | Descripción |
|------|---------|-------------|
| `--headful` | `true` | Navegador con ventana visible |
| `--persist-session` | `true` | Guardar cookies entre ejecuciones |
| `--storage-state-path` | `./state/storage.json` | Archivo de sesión persistida |
| `--db-path` | `./state/manifest.sqlite` | Base de datos SQLite |
| `--output-dir` | `./out` | Directorio de descarga |
| `--rate-limit-seconds` | `1.0` | Pausa entre casos (segundos) |
| `--retries` | `6` | Reintentos por descarga fallida |
| `--timeout-ms` | `60000` | Timeout general (ms) |
| `--start-page` | `1` | Página inicial del listado |
| `--max-pages` | `None` | Límite de páginas a recorrer |
| `--max-items` | `None` | Límite de casos a procesar |
| `--retry-failed` | `false` | Reintentar casos marcados FAILED |
| `--only-missing` | `true` | Solo procesar casos sin descargas |
| `--download-suprema` | `true` | Descargar sección Corte Suprema |
| `--download-apelaciones` | `true` | Descargar sección Corte Apelaciones |
| `--download-tribunales` | `true` | Descargar sección Tribunales |
| `--test-mode` | `false` | Atajo: max-pages=1, max-items=5 |
| `--logs-dir` | `./logs` | Directorio de logs |

### Ejemplos

```bash
# Solo Corte Suprema, primeras 10 páginas
python -m src.main --download-apelaciones false --download-tribunales false --max-pages 10

# Reintentar descargas fallidas
python -m src.main --retry-failed true --only-missing false

# Sin persistir sesión, timeout alto
python -m src.main --persist-session false --timeout-ms 120000

# Empezar desde página 5
python -m src.main --start-page 5 --max-pages 3
```

---

## Estructura de salida

```
out/
  CorteSuprema/
    2024/
      01/              ← hasta 10.000 archivos
      01 (2)/          ← overflow si se superan 10k
      02/
      ...
  CorteApelaciones/
    2024/
      01/
      ...
  Tribunales/
    2024/
      01/
      ...
  meta/
    2024/
      01/
        <case_id>.json  ← metadatos completos + lista de descargas
```

### Regla de 10.000 archivos

Se cuentan archivos reales en el filesystem (no DB). Si una carpeta `MM` alcanza 10.000, se crea `MM (2)`, luego `MM (3)`, etc.

---

## Base de datos SQLite

Ubicación: `./state/manifest.sqlite`

### Tabla `cases`

| Columna | Tipo | Descripción |
|---------|------|-------------|
| case_id | TEXT PK | SHA-1 de la URL "Ver sentencia" |
| url | TEXT | URL completa del caso |
| rol | TEXT | Rol/Nro. de ingreso |
| fecha_sentencia | TEXT | YYYY-MM-DD (nullable) |
| caratulado | TEXT | Carátula del caso |
| corte_origen | TEXT | Corte o tribunal de origen |
| status | TEXT | PENDING / IN_PROGRESS / DONE / FAILED |
| error | TEXT | Mensaje de error si falló |
| page_fingerprint | TEXT | Fingerprint anti-loop |
| last_seen | TEXT | Última vez visto en listado |

### Tabla `downloads`

| Columna | Tipo | Descripción |
|---------|------|-------------|
| id | INTEGER PK | Auto-increment |
| case_id | TEXT FK | Referencia a `cases` |
| section | TEXT | SUPREMA / APELACIONES / TRIBUNALES |
| label | TEXT | sentencia_original, anexo_001, etc. |
| source | TEXT | button / link / newtab / url |
| url | TEXT | URL de descarga (si disponible) |
| filename | TEXT | Nombre del archivo descargado |
| filepath | TEXT | Ruta completa en disco |
| sha256 | TEXT | Hash SHA-256 del archivo |
| bytes | INTEGER | Tamaño en bytes |
| status | TEXT | OK / FAILED / SKIPPED |
| error | TEXT | Mensaje de error |
| downloaded_at | TEXT | Timestamp ISO de descarga |

### Tabla `settings`

Almacena configuración persistente (key-value).

---

## Resume & Deduplicación

El scraper es **reanudable**:

- Los casos se registran como `PENDING` al descubrirlos en el listado.
- Al procesarlos pasan a `IN_PROGRESS` → `DONE` o `FAILED`.
- Si ejecutas de nuevo, solo procesa `PENDING` (o `FAILED` con `--retry-failed true`).
- Las descargas se deduplican por `(case_id, section, label)`.

---

## Troubleshooting

### "No se detectó sesión activa"

- Inicia sesión en el navegador que se abrió.
- Asegúrate de completar todo el flujo de ClaveÚnica.
- Vuelve a la página de búsqueda de juris.pjud.cl.
- Presiona ENTER en la terminal.

### Sesión expira durante el scraping

El script detecta expiración periódicamente. Si ocurre:
1. Pausa automáticamente.
2. Te pide que hagas login nuevamente.
3. Continúa donde quedó.

### "Anti-loop: fingerprint repetido"

La paginación detectó que está mostrando los mismos resultados. Esto puede ocurrir si el sitio tiene un bug o si los filtros cambian. El scraper se detiene para evitar loops infinitos.

### Descargas fallidas

- Revisa `logs/dumps/` para screenshots y HTML del momento del fallo.
- Usa `--retry-failed true` para reintentar.
- Revisa la tabla `downloads` en SQLite para ver errores específicos.

### El dropdown de "50 resultados" no funciona

El scraper intenta múltiples estrategias (select nativo, dropdown custom, URL param). Si falla, continúa con el default y deja screenshot en `logs/dumps/`.

### Error "Playwright not installed"

```bash
playwright install chromium
```

### Performance lenta

- Aumenta `--rate-limit-seconds` si el sitio responde lento.
- Reduce `--timeout-ms` si quieres fallar más rápido.
- Usa `--max-pages` para limitar el alcance.

---

## Desarrollo

```
pjud-scraper/
  src/
    __init__.py
    __main__.py        # Entry point para python -m src
    main.py            # CLI y orquestación principal
    config.py          # Dataclass de configuración + argparse
    logger.py          # Logger dual (archivo + consola)
    db.py              # SQLite schema + helpers CRUD
    selectors.py       # Selectores CSS/XPath y heurísticas
    auth.py            # Detección de sesión + login manual
    listing.py         # Paginación + anti-loop
    case_page.py       # Metadatos + tabs del caso
    downloader.py      # Descarga robusta con fallbacks
    filesystem.py      # Directorios año/mes + límite 10k
    utils.py           # Hashing, sanitización, fechas
  state/               # Storage state + SQLite (gitignored)
  out/                 # Archivos descargados (gitignored)
  logs/                # Logs de ejecución (gitignored)
  requirements.txt
  .gitignore
  README.md
```

---

## Licencia

Uso privado. Este scraper interactúa con un servicio público del Poder Judicial de Chile. Úsalo responsablemente y respeta los términos de servicio del sitio.
