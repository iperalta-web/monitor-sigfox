#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_cron.sh  —  Configura el monitor diario de mensajes Sigfox
# Ejecuta monitor_diario.py todos los días a las 08:00
# Uso:  bash setup_cron.sh
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON=$(which python3)
MONITOR="$SCRIPT_DIR/monitor_diario.py"
LOG="$SCRIPT_DIR/resultados/cron_monitor.log"

echo "========================================"
echo "  Configuración del monitor Sigfox"
echo "========================================"
echo ""
echo "  Directorio : $SCRIPT_DIR"
echo "  Python     : $PYTHON"
echo "  Script     : $MONITOR"
echo "  Log        : $LOG"
echo ""

# Crear directorio de resultados
mkdir -p "$SCRIPT_DIR/resultados"

# ── Método 1: crontab (más simple) ───────────────────────────────────────────
CRON_LINE="0 8 * * * cd '$SCRIPT_DIR' && $PYTHON '$MONITOR' >> '$LOG' 2>&1"

# Verificar si ya existe la línea
EXISTING=$(crontab -l 2>/dev/null | grep -F "monitor_diario.py")
if [ -n "$EXISTING" ]; then
    echo "  [INFO] Ya existe una tarea en crontab:"
    echo "  $EXISTING"
    echo ""
    read -p "  ¿Reemplazar? (s/n): " RESP
    if [ "$RESP" != "s" ]; then
        echo "  Cancelado."
        exit 0
    fi
    # Eliminar línea anterior
    (crontab -l 2>/dev/null | grep -v "monitor_diario.py") | crontab -
fi

# Agregar nueva línea
(crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
echo ""
echo "  ✅ Tarea agregada al crontab:"
echo "  $CRON_LINE"
echo ""
echo "  El monitor correrá todos los días a las 08:00."
echo ""
echo "  Para verificar:   crontab -l"
echo "  Para eliminar:    crontab -e  (y borrar la línea)"
echo ""

# ── Verificar dependencias Python ─────────────────────────────────────────────
echo "  Verificando dependencias Python..."
$PYTHON -c "import pandas, requests, openpyxl" 2>/dev/null
if [ $? -ne 0 ]; then
    echo ""
    echo "  [WARN] Faltan dependencias. Instalar con:"
    echo "  pip3 install pandas requests openpyxl"
    echo ""
fi

echo "  ✅ Listo. Próxima ejecución mañana a las 08:00."
echo ""
echo "  Para probar manualmente ahora:"
echo "  cd '$SCRIPT_DIR' && python3 monitor_diario.py"
echo ""
