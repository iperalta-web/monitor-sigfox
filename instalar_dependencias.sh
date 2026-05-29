#!/bin/bash
echo "Instalando dependencias para Monitor Sigfox..."
pip3 install pandas requests openpyxl streamlit plotly
echo ""
echo "✅ Listo. Para iniciar el dashboard:"
echo ""
echo "   cd '$(cd "$(dirname "$0")" && pwd)'"
echo "   streamlit run dashboard.py"
echo ""
