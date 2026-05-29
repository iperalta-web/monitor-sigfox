#!/usr/bin/env python3
# coding: utf-8
"""
Monitor Diario de Mensajes Sigfox
- Consulta consumo acumulado del mes actual para cada dispositivo
- Compara contra límites configurables
- Envía resumen diario y alertas por email
"""

import json
import os
import sys
import smtplib
import logging
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd
import requests
from requests.auth import HTTPBasicAuth

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("monitor_diario.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def cargar_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def limite_dispositivo(cfg, device_id):
    overrides = cfg["limites"].get("por_dispositivo", {})
    return overrides.get(str(device_id), cfg["limites"]["diario_default"])


def consultar_consumo_mes(login, password, device_id, year, month):
    """Devuelve lista de frameCount por día del mes."""
    url = f"https://api.sigfox.com/v2/devices/{device_id}/consumption/{year}/{month}"
    try:
        r = requests.get(url, auth=HTTPBasicAuth(login, password), timeout=15)
        if r.status_code == 200:
            consumptions = r.json().get("consumption", {}).get("consumptions", [])
            return [c.get("frameCount", 0) for c in consumptions]
        else:
            log.warning(f"  {device_id}: HTTP {r.status_code}")
            return None
    except Exception as e:
        log.error(f"  {device_id}: {e}")
        return None


def construir_tabla(cfg, ids, year, month, dia_hoy):
    login = cfg["sigfox"]["login"]
    password = cfg["sigfox"]["password"]

    filas = []
    for device_id in ids:
        consumos = consultar_consumo_mes(login, password, device_id, year, month)
        if consumos is None:
            continue

        total_mes = sum(consumos)
        dias_con_datos = len(consumos)
        limite_dia = limite_dispositivo(cfg, device_id)
        limite_mes_esperado = limite_dia * dia_hoy
        pct = round(total_mes / limite_mes_esperado * 100, 1) if limite_mes_esperado > 0 else 0

        filas.append({
            "ID Dispositivo": device_id,
            "Año": year,
            "Mes": month,
            "Días con datos": dias_con_datos,
            "Mensajes acumulados": total_mes,
            "Límite diario": limite_dia,
            "Límite esperado al día": limite_mes_esperado,
            "% del límite": pct,
            "Estado": _estado(pct, cfg),
        })
        log.info(f"  {device_id}: {total_mes} msgs | {pct}% del límite")

    return pd.DataFrame(filas)


def _estado(pct, cfg):
    critico = cfg["alertas"]["umbral_critico_pct"]
    advertencia = cfg["alertas"]["umbral_advertencia_pct"]
    if pct >= critico:
        return "🔴 CRÍTICO"
    if pct >= advertencia:
        return "🟡 ADVERTENCIA"
    return "🟢 OK"


# ── Email ─────────────────────────────────────────────────────────────────────

def _smtp_conectar(ecfg):
    if ecfg["usar_tls"]:
        server = smtplib.SMTP(ecfg["smtp_host"], ecfg["smtp_port"])
        server.starttls()
    else:
        server = smtplib.SMTP_SSL(ecfg["smtp_host"], ecfg["smtp_port"])
    server.login(ecfg["usuario"], ecfg["password"])
    return server


def enviar_email(ecfg, destinatarios, asunto, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"] = ecfg["remitente"]
    msg["To"] = ", ".join(destinatarios)
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        server = _smtp_conectar(ecfg)
        server.sendmail(ecfg["remitente"], destinatarios, msg.as_string())
        server.quit()
        log.info(f"  Email enviado a: {destinatarios}")
    except Exception as e:
        log.error(f"  Error al enviar email: {e}")


def _tabla_html(df):
    colores = {"🔴 CRÍTICO": "#ffe0e0", "🟡 ADVERTENCIA": "#fff8d6", "🟢 OK": "#e8f5e9"}
    filas_html = ""
    for _, row in df.iterrows():
        color = colores.get(row["Estado"], "#ffffff")
        filas_html += f"""
        <tr style="background:{color}">
          <td>{row['ID Dispositivo']}</td>
          <td style="text-align:right">{row['Mensajes acumulados']:,}</td>
          <td style="text-align:right">{row['Límite esperado al día']:,}</td>
          <td style="text-align:right">{row['% del límite']}%</td>
          <td style="text-align:right">{row['Límite diario']}</td>
          <td>{row['Estado']}</td>
        </tr>"""
    return f"""
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px">
      <thead style="background:#1a73e8;color:white">
        <tr>
          <th>ID Dispositivo</th>
          <th>Msgs acumulados</th>
          <th>Límite al día de hoy</th>
          <th>% consumido</th>
          <th>Límite/día</th>
          <th>Estado</th>
        </tr>
      </thead>
      <tbody>{filas_html}</tbody>
    </table>"""


def _barra_progreso(pct, color="#1a73e8"):
    fill = min(pct, 100)
    bg = "#ffcccc" if pct >= 90 else "#fff3cd" if pct >= 75 else "#e8f5e9"
    return f"""
    <div style="background:#eee;border-radius:8px;height:24px;width:100%;max-width:500px">
      <div style="background:{color};width:{fill}%;height:24px;border-radius:8px;
                  display:flex;align-items:center;justify-content:center;
                  color:white;font-weight:bold;font-size:13px;min-width:40px">
        {pct}%
      </div>
    </div>"""


def enviar_resumen_diario(cfg, df, year, month, dia, resumen_global=None):
    ecfg = cfg["alertas"]["email"]
    if not ecfg["habilitado"]:
        return
    if not cfg["resumen_diario"]["habilitado"]:
        return

    ok = len(df[df["Estado"] == "🟢 OK"])
    adv = len(df[df["Estado"] == "🟡 ADVERTENCIA"])
    crit = len(df[df["Estado"] == "🔴 CRÍTICO"])

    asunto = f"[Sigfox] Resumen diario {dia:02d}/{month:02d}/{year} — {adv} advertencias, {crit} críticos"
    tabla = _tabla_html(df) if cfg["resumen_diario"]["incluir_tabla_completa"] else ""

    global_html = ""
    if resumen_global:
        pct = resumen_global["pct_del_limite_mensual"]
        color = "#c0392b" if pct >= 90 else "#f39c12" if pct >= 75 else "#27ae60"
        barra = _barra_progreso(pct, color)
        global_html = f"""
        <div style="background:#f8f9fa;border-radius:8px;padding:16px;margin-bottom:20px;
                    border-left:4px solid {color}">
          <h3 style="margin:0 0 12px 0;color:{color}">Consumo Global del Mes</h3>
          <table style="font-family:Arial,sans-serif;font-size:14px;width:100%">
            <tr><td><b>Mensajes consumidos:</b></td>
                <td style="text-align:right"><b>{resumen_global['total_mensajes']:,}</b></td></tr>
            <tr><td><b>Límite mensual:</b></td>
                <td style="text-align:right">{resumen_global['limite_global_mensual']:,}</td></tr>
            <tr><td><b>% del mes consumido:</b></td>
                <td style="text-align:right"><b style="color:{color}">{pct}%</b></td></tr>
            <tr><td><b>Proyección fin de mes:</b></td>
                <td style="text-align:right">{resumen_global['proyeccion_fin_mes']:,}</td></tr>
          </table>
          <div style="margin-top:10px">{barra}</div>
        </div>"""

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:800px">
      <h2 style="color:#1a73e8">Resumen diario de mensajes Sigfox</h2>
      <p><b>Fecha:</b> {dia:02d}/{month:02d}/{year} &nbsp;|&nbsp;
         <b>Dispositivos:</b> {len(df)} &nbsp;|&nbsp;
         🟢 OK: {ok} &nbsp;|&nbsp; 🟡 Advertencia: {adv} &nbsp;|&nbsp; 🔴 Crítico: {crit}</p>
      {global_html}
      {tabla}
      <p style="color:#888;font-size:11px;margin-top:20px">Generado automáticamente por monitor_diario.py</p>
    </body></html>"""

    enviar_email(ecfg, ecfg["destinatarios"], asunto, html)


def enviar_alertas(cfg, df, year, month, dia, resumen_global=None):
    ecfg = cfg["alertas"]["email"]
    if not ecfg["habilitado"]:
        return

    # ── Alerta global ────────────────────────────────────────────────────────
    if resumen_global:
        pct = resumen_global["pct_del_limite_mensual"]
        umb_crit = cfg["alertas"]["umbral_global_critico_pct"]
        umb_adv  = cfg["alertas"]["umbral_global_advertencia_pct"]
        if pct >= umb_crit:
            barra = _barra_progreso(pct, "#c0392b")
            asunto = f"🔴 ALERTA GLOBAL Sigfox — {pct}% del límite de 5M mensajes ({dia:02d}/{month:02d}/{year})"
            html = f"""<html><body style="font-family:Arial,sans-serif;max-width:700px">
              <h2 style="color:#c0392b">⚠️ Alerta Crítica — Límite global de mensajes</h2>
              <p>El consumo total del mes ha alcanzado el <b style="color:#c0392b">{pct}%</b>
                 del límite de <b>{resumen_global['limite_global_mensual']:,}</b> mensajes.</p>
              <table style="font-size:14px;width:100%">
                <tr><td><b>Consumido:</b></td><td style="text-align:right"><b>{resumen_global['total_mensajes']:,}</b></td></tr>
                <tr><td><b>Límite:</b></td><td style="text-align:right">{resumen_global['limite_global_mensual']:,}</td></tr>
                <tr><td><b>Proyección fin de mes:</b></td><td style="text-align:right">{resumen_global['proyeccion_fin_mes']:,}</td></tr>
              </table>
              <div style="margin-top:12px">{barra}</div>
              <p style="color:#888;font-size:11px;margin-top:20px">Generado automáticamente por monitor_diario.py</p>
            </body></html>"""
            enviar_email(ecfg, ecfg["destinatarios_criticos"], asunto, html)
        elif pct >= umb_adv:
            barra = _barra_progreso(pct, "#f39c12")
            asunto = f"🟡 Advertencia Global Sigfox — {pct}% del límite de 5M mensajes ({dia:02d}/{month:02d}/{year})"
            html = f"""<html><body style="font-family:Arial,sans-serif;max-width:700px">
              <h2 style="color:#f39c12">⚠️ Advertencia — Consumo global elevado</h2>
              <p>El consumo total del mes ha superado el <b style="color:#f39c12">{pct}%</b>
                 del límite de <b>{resumen_global['limite_global_mensual']:,}</b> mensajes.</p>
              <table style="font-size:14px;width:100%">
                <tr><td><b>Consumido:</b></td><td style="text-align:right"><b>{resumen_global['total_mensajes']:,}</b></td></tr>
                <tr><td><b>Proyección fin de mes:</b></td><td style="text-align:right">{resumen_global['proyeccion_fin_mes']:,}</td></tr>
              </table>
              <div style="margin-top:12px">{barra}</div>
              <p style="color:#888;font-size:11px;margin-top:20px">Generado automáticamente por monitor_diario.py</p>
            </body></html>"""
            enviar_email(ecfg, ecfg["destinatarios"], asunto, html)

    criticos = df[df["Estado"] == "🔴 CRÍTICO"]
    advertencias = df[df["Estado"] == "🟡 ADVERTENCIA"]

    if criticos.empty and advertencias.empty:
        log.info("  Sin alertas de dispositivos individuales.")
        return

    if not criticos.empty:
        tabla = _tabla_html(criticos)
        asunto = f"🔴 ALERTA CRÍTICA Sigfox — {len(criticos)} dispositivos al límite ({dia:02d}/{month:02d}/{year})"
        html = f"""
        <html><body style="font-family:Arial,sans-serif">
          <h2 style="color:#c0392b">⚠️ Alerta Crítica — Límite de mensajes alcanzado</h2>
          <p>{len(criticos)} dispositivo(s) han alcanzado o superado el
             {cfg['alertas']['umbral_critico_pct']}% del límite diario acumulado.</p>
          {tabla}
          <p style="color:#888;font-size:11px;margin-top:20px">Generado automáticamente por monitor_diario.py</p>
        </body></html>"""
        enviar_email(ecfg, ecfg["destinatarios_criticos"], asunto, html)

    if not advertencias.empty:
        tabla = _tabla_html(advertencias)
        asunto = f"🟡 Advertencia Sigfox — {len(advertencias)} dispositivos al {cfg['alertas']['umbral_advertencia_pct']}% ({dia:02d}/{month:02d}/{year})"
        html = f"""
        <html><body style="font-family:Arial,sans-serif">
          <h2 style="color:#f39c12">⚠️ Advertencia — Consumo de mensajes elevado</h2>
          <p>{len(advertencias)} dispositivo(s) superaron el
             {cfg['alertas']['umbral_advertencia_pct']}% del límite diario acumulado.</p>
          {tabla}
          <p style="color:#888;font-size:11px;margin-top:20px">Generado automáticamente por monitor_diario.py</p>
        </body></html>"""
        enviar_email(ecfg, ecfg["destinatarios"], asunto, html)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    cfg = cargar_config()
    hoy = date.today()
    year = str(hoy.year)
    month = str(hoy.month)
    dia = hoy.day

    devices_file = os.path.join(SCRIPT_DIR, cfg["archivos"]["lista_devices"])
    col_ids = cfg["archivos"]["columna_ids"]
    ids = pd.read_csv(devices_file)[col_ids].tolist()

    log.info(f"=== Monitor Sigfox {dia:02d}/{month}/{year} — {len(ids)} dispositivos ===")

    df = construir_tabla(cfg, ids, year, month, dia)
    if df.empty:
        log.warning("No se obtuvo información de ningún dispositivo.")
        sys.exit(1)

    # Guardar CSV del día
    resultados_dir = os.path.join(SCRIPT_DIR, cfg["archivos"]["directorio_resultados"])
    os.makedirs(resultados_dir, exist_ok=True)
    csv_path = os.path.join(resultados_dir, f"monitor_{year}_{int(month):02d}_{dia:02d}.csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    log.info(f"  Resultado guardado en: {csv_path}")

    # ── Totales globales ──────────────────────────────────────────────────────
    total_global = int(df["Mensajes acumulados"].sum())
    limite_global = cfg["limites"]["global_mensual"]
    dias_mes = __import__("calendar").monthrange(int(year), int(month))[1]
    limite_global_hoy = round(limite_global * (dia / dias_mes))
    pct_global = round(total_global / limite_global * 100, 2)
    pct_global_hoy = round(total_global / limite_global_hoy * 100, 2) if limite_global_hoy else 0
    proyeccion = round(total_global / dia * dias_mes) if dia > 0 else 0

    # Guardar resumen global en JSON para el dashboard
    resumen_global = {
        "fecha": str(hoy),
        "year": year,
        "month": month,
        "dia": dia,
        "total_mensajes": total_global,
        "limite_global_mensual": limite_global,
        "limite_global_al_dia": limite_global_hoy,
        "pct_del_limite_mensual": pct_global,
        "pct_del_limite_al_dia": pct_global_hoy,
        "proyeccion_fin_mes": proyeccion,
        "dispositivos": len(df),
        "criticos": int(len(df[df["Estado"] == "🔴 CRÍTICO"])),
        "advertencias": int(len(df[df["Estado"] == "🟡 ADVERTENCIA"])),
        "ok": int(len(df[df["Estado"] == "🟢 OK"])),
    }
    with open(os.path.join(resultados_dir, "ultimo_monitor.json"), "w") as f:
        json.dump(resumen_global, f, indent=2)

    # Resumen en consola
    print("\n" + df.to_string(index=False) + "\n")
    crit = len(df[df["Estado"] == "🔴 CRÍTICO"])
    adv = len(df[df["Estado"] == "🟡 ADVERTENCIA"])
    print(f"━━━ GLOBAL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  Total mensajes mes   : {total_global:>12,}  /  {limite_global:,}  ({pct_global}%)")
    print(f"  Límite esperado hoy  : {limite_global_hoy:>12,}  →  {pct_global_hoy}% del ritmo esperado")
    print(f"  Proyección fin de mes: {proyeccion:>12,}")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"Dispositivos: 🟢 OK={resumen_global['ok']}  🟡 Advertencia={adv}  🔴 Crítico={crit}\n")

    # Emails
    enviar_resumen_diario(cfg, df, year, month, dia, resumen_global)
    enviar_alertas(cfg, df, year, month, dia, resumen_global)

    log.info("=== Listo ===")


if __name__ == "__main__":
    main()
