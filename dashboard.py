#!/usr/bin/env python3
# coding: utf-8
"""
Dashboard Sigfox — Monitor de Mensajes
Ejecutar:  streamlit run dashboard.py
"""

import json
import os
import calendar
from datetime import date, datetime

import pandas as pd
import requests
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
from requests.auth import HTTPBasicAuth

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

st.set_page_config(
    page_title="Monitor Sigfox",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Estilos ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  .metric-card {
    background: #f8f9fa; border-radius: 12px; padding: 20px;
    border-left: 5px solid #1a73e8; margin-bottom: 10px;
  }
  .alerta-critica { border-left-color: #c0392b !important; background: #fff5f5 !important; }
  .alerta-advertencia { border-left-color: #f39c12 !important; background: #fffdf0 !important; }
  .alerta-ok { border-left-color: #27ae60 !important; background: #f0fff4 !important; }
  .titulo-seccion { color: #1a73e8; font-size: 18px; font-weight: bold; margin: 16px 0 8px 0; }
</style>
""", unsafe_allow_html=True)


# ── Config ────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=0)
def cargar_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def guardar_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    st.cache_data.clear()


# ── API Sigfox ────────────────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def consultar_consumo(login, password, device_id, year, month):
    url = f"https://api.sigfox.com/v2/devices/{device_id}/consumption/{year}/{month}"
    try:
        r = requests.get(url, auth=HTTPBasicAuth(login, password), timeout=15)
        if r.status_code == 200:
            return r.json().get("consumption", {}).get("consumptions", [])
    except Exception:
        pass
    return None


@st.cache_data(ttl=300, show_spinner=False)
def cargar_datos(login, password, ids, year, month):
    dias_mes = calendar.monthrange(int(year), int(month))[1]
    registros = []
    for device_id in ids:
        consumptions = consultar_consumo(login, password, device_id, year, month)
        if consumptions is None:
            continue
        diarios = []
        for d in range(dias_mes):
            val = consumptions[d]["frameCount"] if d < len(consumptions) else None
            diarios.append(val)
        total = sum(v for v in diarios if v is not None)
        registros.append({"id": device_id, "diarios": diarios, "total": total})
    return registros, dias_mes


# ── Helpers ───────────────────────────────────────────────────────────────────
def estado_color(pct, cfg):
    if pct >= cfg["alertas"]["umbral_critico_pct"]:
        return "🔴", "#c0392b", "CRÍTICO"
    if pct >= cfg["alertas"]["umbral_advertencia_pct"]:
        return "🟡", "#f39c12", "ADVERTENCIA"
    return "🟢", "#27ae60", "OK"


def gauge(valor, limite, titulo, color_umbral_adv, color_umbral_crit):
    pct = valor / limite * 100 if limite else 0
    color = "#c0392b" if pct >= color_umbral_crit else \
            "#f39c12" if pct >= color_umbral_adv else "#27ae60"
    fig = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=valor,
        delta={"reference": limite, "valueformat": ",.0f",
               "decreasing": {"color": "#27ae60"}, "increasing": {"color": "#c0392b"}},
        number={"valueformat": ",.0f", "font": {"size": 28}},
        title={"text": titulo, "font": {"size": 14}},
        gauge={
            "axis": {"range": [0, limite], "tickformat": ",.0f",
                     "nticks": 6, "tickfont": {"size": 10}},
            "bar": {"color": color, "thickness": 0.75},
            "bgcolor": "#f0f0f0",
            "borderwidth": 0,
            "steps": [
                {"range": [0, limite * color_umbral_adv / 100], "color": "#e8f5e9"},
                {"range": [limite * color_umbral_adv / 100, limite * color_umbral_crit / 100], "color": "#fff8d6"},
                {"range": [limite * color_umbral_crit / 100, limite], "color": "#ffe0e0"},
            ],
            "threshold": {
                "line": {"color": "#c0392b", "width": 3},
                "thickness": 0.85,
                "value": limite,
            },
        },
    ))
    fig.update_layout(height=260, margin=dict(l=20, r=20, t=40, b=10))
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR — Configuración
# ══════════════════════════════════════════════════════════════════════════════
def sidebar(cfg):
    st.sidebar.image("https://www.sigfox.com/themes/custom/sigfox/logo.svg",
                     width=140, use_column_width=False)
    st.sidebar.title("⚙️ Configuración")

    hoy = date.today()

    with st.sidebar.expander("📅 Período", expanded=True):
        year  = st.selectbox("Año",  list(range(hoy.year, hoy.year - 3, -1)), index=0)
        month = st.selectbox("Mes",  list(range(1, 13)), index=hoy.month - 1,
                             format_func=lambda m: calendar.month_name[m])

    with st.sidebar.expander("🎯 Límites", expanded=True):
        lim_global = st.number_input(
            "Límite global mensual",
            min_value=1000, max_value=100_000_000,
            value=cfg["limites"]["global_mensual"], step=100_000,
            help="Total de mensajes permitidos para TODOS los dispositivos en el mes",
        )
        lim_diario = st.number_input(
            "Límite diario por dispositivo",
            min_value=1, max_value=10_000,
            value=cfg["limites"]["diario_default"], step=10,
        )
        adv_pct  = st.slider("% Advertencia individual", 50, 99, cfg["alertas"]["umbral_advertencia_pct"])
        crit_pct = st.slider("% Crítico individual",     51, 100, cfg["alertas"]["umbral_critico_pct"])
        adv_g    = st.slider("% Advertencia global",     50, 99, cfg["alertas"].get("umbral_global_advertencia_pct", 75))
        crit_g   = st.slider("% Crítico global",         51, 100, cfg["alertas"].get("umbral_global_critico_pct", 90))

        if st.button("💾 Guardar límites"):
            cfg["limites"]["global_mensual"]  = lim_global
            cfg["limites"]["diario_default"]  = lim_diario
            cfg["alertas"]["umbral_advertencia_pct"] = adv_pct
            cfg["alertas"]["umbral_critico_pct"]     = crit_pct
            cfg["alertas"]["umbral_global_advertencia_pct"] = adv_g
            cfg["alertas"]["umbral_global_critico_pct"]     = crit_g
            guardar_config(cfg)
            st.success("Configuración guardada")
            st.rerun()

    with st.sidebar.expander("📧 Email alertas"):
        ecfg = cfg["alertas"]["email"]
        hab = st.toggle("Alertas por email", value=ecfg["habilitado"])
        dest = st.text_area("Destinatarios (uno por línea)",
                            value="\n".join(ecfg["destinatarios"]))
        if st.button("💾 Guardar email"):
            cfg["alertas"]["email"]["habilitado"] = hab
            cfg["alertas"]["email"]["destinatarios"] = [d.strip() for d in dest.splitlines() if d.strip()]
            guardar_config(cfg)
            st.success("Guardado")

    if st.sidebar.button("🔄 Refrescar datos", type="primary", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    return year, month, cfg


# ══════════════════════════════════════════════════════════════════════════════
# MAIN DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
def main():
    cfg = cargar_config()
    year, month, cfg = sidebar(cfg)

    hoy = date.today()
    dia_hoy = hoy.day if (year == hoy.year and month == hoy.month) else \
              calendar.monthrange(year, month)[1]

    st.title("📡 Monitor de Mensajes Sigfox")
    st.caption(f"Actualizado: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}  |  "
               f"Período: {calendar.month_name[month]} {year}")

    # ── Cargar datos ──────────────────────────────────────────────────────────
    devices_file = os.path.join(SCRIPT_DIR, cfg["archivos"]["lista_devices"])
    col_ids = cfg["archivos"]["columna_ids"]

    try:
        ids = pd.read_csv(devices_file)[col_ids].tolist()
    except Exception as e:
        st.error(f"No se pudo leer {devices_file}: {e}")
        return

    with st.spinner(f"Consultando {len(ids)} dispositivos en Sigfox..."):
        registros, dias_mes = cargar_datos(
            cfg["sigfox"]["login"], cfg["sigfox"]["password"],
            tuple(ids), str(year), str(month),
        )

    if not registros:
        st.error("No se obtuvo información de la API de Sigfox.")
        return

    limite_global = cfg["limites"]["global_mensual"]
    limite_diario = cfg["limites"]["diario_default"]
    adv_g  = cfg["alertas"].get("umbral_global_advertencia_pct", 75)
    crit_g = cfg["alertas"].get("umbral_global_critico_pct", 90)

    total_global = sum(r["total"] for r in registros)
    pct_global   = round(total_global / limite_global * 100, 2) if limite_global else 0

    # ── Banner prominente del pool ────────────────────────────────────────────
    if pct_global >= crit_g:
        banner_color = "#c0392b"
        banner_bg    = "#fff0f0"
        banner_borde = "#e74c3c"
        banner_icono = "🔴"
        banner_label = "CRÍTICO"
    elif pct_global >= adv_g:
        banner_color = "#b7610a"
        banner_bg    = "#fffbe6"
        banner_borde = "#f39c12"
        banner_icono = "🟡"
        banner_label = "ADVERTENCIA"
    else:
        banner_color = "#1a6b34"
        banner_bg    = "#f0fdf4"
        banner_borde = "#27ae60"
        banner_icono = "🟢"
        banner_label = "NORMAL"

    barra_fill  = min(pct_global, 100)
    barra_color = banner_borde
    consumidos_fmt  = f"{total_global:,}"
    limite_fmt      = f"{limite_global:,}"
    disponibles_fmt = f"{max(limite_global - total_global, 0):,}"

    st.markdown(f"""
    <div style="background:{banner_bg};border:2px solid {banner_borde};border-radius:14px;
                padding:22px 28px;margin-bottom:24px">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
        <div>
          <div style="font-size:13px;color:#666;font-family:Arial;text-transform:uppercase;
                      letter-spacing:1px;margin-bottom:4px">
            Pool de mensajes — {calendar.month_name[month]} {year}
          </div>
          <div style="font-size:52px;font-weight:900;color:{banner_color};
                      font-family:Arial;line-height:1">
            {pct_global}%
          </div>
          <div style="font-size:15px;color:{banner_color};font-family:Arial;margin-top:4px;font-weight:bold">
            {banner_icono} {banner_label}
          </div>
        </div>
        <div style="text-align:right;font-family:Arial">
          <div style="font-size:13px;color:#888">Consumidos</div>
          <div style="font-size:24px;font-weight:bold;color:{banner_color}">{consumidos_fmt}</div>
          <div style="font-size:13px;color:#888;margin-top:8px">Límite del pool</div>
          <div style="font-size:18px;font-weight:bold;color:#333">{limite_fmt}</div>
          <div style="font-size:13px;color:#888;margin-top:8px">Disponibles</div>
          <div style="font-size:18px;font-weight:bold;color:#27ae60">{disponibles_fmt}</div>
        </div>
      </div>
      <div style="margin-top:16px">
        <div style="background:#ddd;border-radius:999px;height:18px;width:100%;overflow:hidden">
          <div style="background:{barra_color};width:{barra_fill}%;height:18px;border-radius:999px;
                      transition:width 0.5s ease;display:flex;align-items:center;
                      justify-content:flex-end;padding-right:8px">
            <span style="color:white;font-size:11px;font-weight:bold;white-space:nowrap">
              {pct_global}%
            </span>
          </div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:11px;
                    color:#999;margin-top:4px;font-family:Arial">
          <span>0</span>
          <span style="color:{banner_borde};font-weight:bold">
            Umbral advertencia {adv_g}%
          </span>
          <span style="color:{banner_borde};font-weight:bold">
            Crítico {crit_g}%
          </span>
          <span>{limite_fmt}</span>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)
    limite_hoy   = round(limite_global * dia_hoy / dias_mes)
    pct_ritmo    = round(total_global / limite_hoy * 100, 2) if limite_hoy else 0
    proyeccion   = round(total_global / dia_hoy * dias_mes) if dia_hoy else 0
    dias_restantes = dias_mes - dia_hoy
    presupuesto_restante = limite_global - total_global
    ritmo_necesario = round(presupuesto_restante / dias_restantes) if dias_restantes > 0 else 0

    # ── Sección 1: KPIs globales ──────────────────────────────────────────────
    st.markdown("### 🌐 Consumo Global del Mes")
    col1, col2 = st.columns([1.4, 1])

    with col1:
        fig_gauge = gauge(total_global, limite_global,
                          f"Total mensajes — {calendar.month_name[month]} {year}",
                          adv_g, crit_g)
        st.plotly_chart(fig_gauge, use_container_width=True)

    with col2:
        icono, color_g, estado_g = estado_color(pct_global, {
            "alertas": {"umbral_advertencia_pct": adv_g, "umbral_critico_pct": crit_g}
        })
        card_class = "alerta-critica" if estado_g == "CRÍTICO" else \
                     "alerta-advertencia" if estado_g == "ADVERTENCIA" else "alerta-ok"

        st.markdown(f"""
        <div class="metric-card {card_class}">
          <div style="font-size:28px;font-weight:bold;color:{color_g}">{icono} {estado_g}</div>
          <div style="font-size:34px;font-weight:bold;margin:8px 0">{pct_global}%</div>
          <div style="font-size:13px;color:#555">del límite mensual consumido</div>
        </div>""", unsafe_allow_html=True)

        st.metric("Mensajes consumidos",  f"{total_global:,}")
        st.metric("Límite mensual",       f"{limite_global:,}")
        st.metric("Proyección fin de mes",f"{proyeccion:,}",
                  delta=f"{proyeccion - limite_global:+,} vs límite",
                  delta_color="inverse")
        st.metric("% del ritmo esperado hoy", f"{pct_ritmo}%")

    # ── KPIs secundarios ──────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Días transcurridos",    f"{dia_hoy} / {dias_mes}")
    c2.metric("Días restantes",        f"{dias_restantes}")
    c3.metric("Mensajes disponibles",  f"{presupuesto_restante:,}")
    c4.metric("Ritmo máx. necesario",  f"{ritmo_necesario:,}/día",
              help="Mensajes totales por día para NO superar el límite")

    st.divider()

    # ── Sección 2: Acumulado diario ────────────────────────────────────────────
    st.markdown("### 📈 Acumulado Diario Global")

    # Armar serie acumulada sumando todos los dispositivos por día
    serie_diaria = [0.0] * dias_mes
    for rec in registros:
        for d, v in enumerate(rec["diarios"]):
            if v is not None:
                serie_diaria[d] += v

    dias_labels = list(range(1, dias_mes + 1))
    acum = []
    acc = 0
    for v in serie_diaria:
        acc += v
        acum.append(acc)

    # Línea de ritmo esperado
    ritmo_esperado = [limite_global * (d / dias_mes) for d in dias_labels]
    limite_linea   = [limite_global] * dias_mes

    fig_line = go.Figure()
    fig_line.add_trace(go.Scatter(
        x=dias_labels, y=acum, name="Consumo acumulado",
        line=dict(color="#1a73e8", width=3),
        fill="tozeroy", fillcolor="rgba(26,115,232,0.08)",
    ))
    fig_line.add_trace(go.Scatter(
        x=dias_labels, y=ritmo_esperado, name="Ritmo esperado",
        line=dict(color="#27ae60", width=2, dash="dot"),
    ))
    fig_line.add_trace(go.Scatter(
        x=dias_labels, y=limite_linea, name="Límite mensual",
        line=dict(color="#c0392b", width=2, dash="dash"),
    ))
    # Proyección del día actual al fin de mes
    if dia_hoy < dias_mes and total_global > 0:
        ritmo_actual = total_global / dia_hoy
        proy_x = list(range(dia_hoy, dias_mes + 1))
        proy_y = [total_global + ritmo_actual * (d - dia_hoy) for d in proy_x]
        fig_line.add_trace(go.Scatter(
            x=proy_x, y=proy_y, name="Proyección",
            line=dict(color="#f39c12", width=2, dash="longdash"),
        ))

    fig_line.update_layout(
        height=320, margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(orientation="h", y=1.02),
        yaxis=dict(tickformat=",.0f"),
        hovermode="x unified",
    )
    st.plotly_chart(fig_line, use_container_width=True)

    # ── Sección 3: Mensajes por día (barras) ───────────────────────────────────
    st.markdown("### 📊 Mensajes por Día (todos los dispositivos)")
    colores_barra = [
        "#c0392b" if v > limite_diario * len(registros) else
        "#f39c12" if v > limite_diario * len(registros) * 0.8 else
        "#1a73e8"
        for v in serie_diaria[:dia_hoy]
    ]
    fig_bar = go.Figure(go.Bar(
        x=dias_labels[:dia_hoy],
        y=serie_diaria[:dia_hoy],
        marker_color=colores_barra,
        name="Mensajes del día",
    ))
    fig_bar.update_layout(
        height=260, margin=dict(l=10, r=10, t=10, b=10),
        yaxis=dict(tickformat=",.0f"),
        hovermode="x unified",
    )
    st.plotly_chart(fig_bar, use_container_width=True)

    st.divider()

    # ── Sección 4: Tabla por dispositivo ─────────────────────────────────────
    st.markdown("### 📋 Detalle por Dispositivo")

    filas = []
    for rec in registros:
        lim_dev = cfg["limites"]["por_dispositivo"].get(str(rec["id"]), limite_diario)
        lim_mes_dev = lim_dev * dias_mes
        pct_dev = round(rec["total"] / lim_mes_dev * 100, 1) if lim_mes_dev else 0
        icono_d, _, estado_d = estado_color(pct_dev, cfg)
        filas.append({
            "Estado": f"{icono_d} {estado_d}",
            "ID Dispositivo": str(rec["id"]),
            "Mensajes acumulados": rec["total"],
            "Límite mensual": lim_mes_dev,
            "% del límite": pct_dev,
            "Límite/día": lim_dev,
            "Promedio/día": round(rec["total"] / dia_hoy, 1) if dia_hoy else 0,
            "Días con datos": sum(1 for v in rec["diarios"] if v is not None),
        })

    df_tabla = pd.DataFrame(filas).sort_values("Mensajes acumulados", ascending=False)

    # Filtros rápidos
    col_f1, col_f2 = st.columns([2, 1])
    filtro = col_f1.multiselect("Filtrar por estado", ["🔴 CRÍTICO", "🟡 ADVERTENCIA", "🟢 OK"],
                                 default=["🔴 CRÍTICO", "🟡 ADVERTENCIA", "🟢 OK"])
    buscar = col_f2.text_input("Buscar ID dispositivo", "")

    df_filtrado = df_tabla[df_tabla["Estado"].str.contains("|".join(f.split()[1] for f in filtro) if filtro else "OK|ADVERTENCIA|CRÍTICO")]
    if buscar:
        df_filtrado = df_filtrado[df_filtrado["ID Dispositivo"].str.contains(buscar, case=False)]

    def color_estado(val):
        if "CRÍTICO" in str(val):     return "background-color: #ffe0e0; color: #c0392b; font-weight:bold"
        if "ADVERTENCIA" in str(val): return "background-color: #fff8d6; color: #f39c12; font-weight:bold"
        return "background-color: #e8f5e9; color: #27ae60"

    def color_pct(val):
        if isinstance(val, float):
            if val >= cfg["alertas"]["umbral_critico_pct"]:     return "color: #c0392b; font-weight:bold"
            if val >= cfg["alertas"]["umbral_advertencia_pct"]: return "color: #f39c12; font-weight:bold"
        return ""

    styled = df_filtrado.style \
        .applymap(color_estado, subset=["Estado"]) \
        .applymap(color_pct,    subset=["% del límite"]) \
        .format({"Mensajes acumulados": "{:,}", "Límite mensual": "{:,}",
                 "% del límite": "{:.1f}%", "Promedio/día": "{:.1f}"})

    st.dataframe(styled, use_container_width=True, height=400)

    # Resumen rápido abajo
    crit_n = sum(1 for f in filas if "CRÍTICO" in f["Estado"])
    adv_n  = sum(1 for f in filas if "ADVERTENCIA" in f["Estado"])
    ok_n   = len(filas) - crit_n - adv_n
    c1, c2, c3 = st.columns(3)
    c1.metric("🟢 OK",          ok_n)
    c2.metric("🟡 Advertencia", adv_n)
    c3.metric("🔴 Crítico",     crit_n)

    # ── Sección 5: Top 10 consumidores ────────────────────────────────────────
    st.divider()
    st.markdown("### 🏆 Top 10 Dispositivos con Mayor Consumo")
    top10 = df_tabla.head(10)
    fig_top = px.bar(
        top10, x="ID Dispositivo", y="Mensajes acumulados",
        color="% del límite",
        color_continuous_scale=["#27ae60", "#f39c12", "#c0392b"],
        range_color=[0, 100],
        text="Mensajes acumulados",
    )
    fig_top.update_traces(texttemplate="%{text:,}", textposition="outside")
    fig_top.update_layout(height=350, margin=dict(l=10, r=10, t=10, b=10),
                          coloraxis_colorbar=dict(title="% límite"))
    st.plotly_chart(fig_top, use_container_width=True)


if __name__ == "__main__":
    main()
