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
echo ""
echo "━━━ COPIA ESTE VALOR COMO SECRET 'SESSION_JSON' EN GITHUB ━━━"
echo ""
base64 -i "$SESSION_FILE" | tr -d '\n'
echo ""
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Pasos en GitHub:"
echo "  1. Ve a tu repositorio → Settings → Secrets and variables → Actions"
echo "  2. Haz clic en 'New repository secret'"
echo "  3. Nombre:  SESSION_JSON"
echo "  4. Valor:   pega el texto de arriba (todo en una sola línea)"
echo "  5. Haz clic en 'Add secret'"
echo ""
echo "⚠️  La sesión de ClaveÚnica expira. Cuando el workflow falle con"
echo "   'SESIÓN INVÁLIDA', repite este proceso para renovarla."
