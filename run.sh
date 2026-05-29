#!/bin/bash
cd "$(dirname "$0")"
echo ""
echo "  📡 Monitor Sigfox — Iniciando servidor web..."
echo "  Abre en tu navegador: http://localhost:5000"
echo ""
pip3 install flask pandas requests openpyxl -q
python3 app.py
