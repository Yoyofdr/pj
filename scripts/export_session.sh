#!/usr/bin/env bash
# =============================================================================
# export_session.sh
#
# Exporta la sesión de ClaveÚnica para usarla como GitHub Secret.
#
# Uso:
#   1. Ejecuta el scraper localmente y haz login con ClaveÚnica:
#        python -m src.main --max-pages 1 --max-items 1
#
#   2. Ejecuta este script desde la raíz del proyecto (pjud-scraper/):
#        bash scripts/export_session.sh
#
#   3. Copia el valor impreso y pégalo como nuevo secret en GitHub:
#        Repositorio → Settings → Secrets and variables → Actions
#        → New repository secret → Nombre: SESSION_JSON
# =============================================================================

set -euo pipefail

SESSION_FILE="state/storage.json"

if [ ! -f "$SESSION_FILE" ]; then
  echo "❌  No se encontró $SESSION_FILE"
  echo ""
  echo "   Primero ejecuta el scraper localmente para generar la sesión:"
  echo "     python -m src.main --max-pages 1 --max-items 1"
  exit 1
fi

echo "✅  Sesión encontrada: $SESSION_FILE"

# Exportar a archivo temporal y copiar al portapapeles
TMPFILE=$(mktemp)
base64 -i "$SESSION_FILE" | tr -d '\n' > "$TMPFILE"

echo ""
echo "✅  Base64 generado ($(wc -c < "$TMPFILE") caracteres)"

if command -v pbcopy &>/dev/null; then
  cat "$TMPFILE" | pbcopy
  echo "✅  Copiado al portapapeles de macOS."
  echo ""
  echo "━━━ PASOS EN GITHUB ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  echo "  1. Abre: https://github.com/$(git remote get-url origin | sed 's/.*github.com\///;s/\.git//')/settings/secrets/actions"
  echo "  2. Edita (o crea) el secret  SESSION_JSON"
  echo "  3. Pega con Cmd+V  (ya está en el portapapeles)"
  echo "  4. Guarda"
  echo ""
else
  echo ""
  echo "━━━ COPIA ESTE VALOR COMO SECRET 'SESSION_JSON' EN GITHUB ━━━"
  echo ""
  cat "$TMPFILE"
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
fi

rm -f "$TMPFILE"

echo "⚠️  La sesión de ClaveÚnica expira. Cuando el workflow falle con"
echo "   'SESIÓN INVÁLIDA', repite este proceso para renovarla."
