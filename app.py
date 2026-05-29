#!/usr/bin/env python3
# coding: utf-8
"""
Monitor Sigfox — Web App con autenticación
Ejecutar:  python3 app.py
"""

import csv
import io
import json
import os
import calendar
import smtplib
import time
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from functools import wraps

import pandas as pd
import requests
from flask import (Flask, jsonify, render_template, request,
                   redirect, url_for, session, flash, Response)
from requests.auth import HTTPBasicAuth
from apscheduler.schedulers.background import BackgroundScheduler

from database import (init_db, verificar_usuario, get_usuario, get_config,
                      set_config, actualizar_ultimo_login, listar_usuarios,
                      crear_usuario, actualizar_usuario, eliminar_usuario,
                      contar_usuarios_activos)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sigfox-monitor-secret-2024-iotnet")

# Inicializar DB siempre al arrancar (con o sin gunicorn)
init_db()

# ── Cache en memoria (5 min) ──────────────────────────────────────────────────
_cache = {}
CACHE_TTL = 300

def cache_get(key):
    e = _cache.get(key)
    return e["data"] if e and (time.time() - e["ts"]) < CACHE_TTL else None

def cache_set(key, data):
    _cache[key] = {"ts": time.time(), "data": data}

# ── Auth helpers ──────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        u = get_usuario(session["user_id"])
        if not u or u["rol"] != "admin":
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated

def usuario_actual():
    if "user_id" in session:
        return get_usuario(session["user_id"])
    return None

# ── Config ────────────────────────────────────────────────────────────────────
def cargar_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def guardar_config_json(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

# ── Sigfox API ────────────────────────────────────────────────────────────────
def consultar_consumo_api(login, password, device_id, year, month):
    url = f"https://api.sigfox.com/v2/devices/{device_id}/consumption/{year}/{month}"
    try:
        r = requests.get(url, auth=HTTPBasicAuth(login, password), timeout=15)
        if r.status_code == 200:
            return r.json().get("consumption", {}).get("consumptions", [])
    except Exception as e:
        print(f"  [ERR] {device_id}: {e}")
    return None

def obtener_datos(year, month, force=False):
    cache_key = f"{year}_{month}"
    if not force:
        cached = cache_get(cache_key)
        if cached:
            return cached

    cfg = cargar_config()
    devices_file = os.path.join(SCRIPT_DIR, cfg["archivos"]["lista_devices"])
    ids = pd.read_csv(devices_file)[cfg["archivos"]["columna_ids"]].tolist()

    dias_mes = calendar.monthrange(int(year), int(month))[1]
    hoy = date.today()
    dia_corte = hoy.day if (int(year) == hoy.year and int(month) == hoy.month) else dias_mes

    registros = []
    for device_id in ids:
        consumptions = consultar_consumo_api(
            cfg["sigfox"]["login"], cfg["sigfox"]["password"], device_id, year, month)
        if consumptions is None:
            continue
        diarios = [consumptions[d]["frameCount"] if d < len(consumptions) else None
                   for d in range(dias_mes)]
        registros.append({"id": str(device_id), "diarios": diarios,
                           "total": sum(v for v in diarios if v is not None)})

    limite_global = cfg["limites"]["global_mensual"]
    limite_diario = cfg["limites"]["diario_default"]
    total_global  = sum(r["total"] for r in registros)
    pct_global    = round(total_global / limite_global * 100, 2) if limite_global else 0
    limite_hoy    = round(limite_global * dia_corte / dias_mes)
    pct_ritmo     = round(total_global / limite_hoy * 100, 2) if limite_hoy else 0
    proyeccion    = round(total_global / dia_corte * dias_mes) if dia_corte else 0
    dias_rest     = dias_mes - dia_corte
    ritmo_nec     = round((limite_global - total_global) / dias_rest) if dias_rest > 0 else 0
    adv_g  = cfg["alertas"].get("umbral_global_advertencia_pct", 75)
    crit_g = cfg["alertas"].get("umbral_global_critico_pct", 90)

    dispositivos = []
    for rec in registros:
        lim_dev = cfg["limites"]["por_dispositivo"].get(rec["id"], limite_diario)
        lim_mes = lim_dev * dias_mes
        pct_dev = round(rec["total"] / lim_mes * 100, 1) if lim_mes else 0
        if pct_dev >= cfg["alertas"]["umbral_critico_pct"]:      estado = "CRITICO"
        elif pct_dev >= cfg["alertas"]["umbral_advertencia_pct"]: estado = "ADVERTENCIA"
        else:                                                      estado = "OK"
        dispositivos.append({
            "id": rec["id"], "total": rec["total"],
            "limite_mes": lim_mes, "limite_dia": lim_dev,
            "pct": pct_dev, "estado": estado,
            "promedio_dia": round(rec["total"] / dia_corte, 1) if dia_corte else 0,
            "dias_datos": sum(1 for v in rec["diarios"] if v is not None),
            "diarios": rec["diarios"],
        })
    dispositivos.sort(key=lambda x: x["total"], reverse=True)

    serie_global = [0.0] * dias_mes
    for rec in registros:
        for d, v in enumerate(rec["diarios"]):
            if v is not None:
                serie_global[d] += v

    resultado = {
        "year": year, "month": month,
        "mes_nombre": calendar.month_name[int(month)],
        "dia_corte": dia_corte, "dias_mes": dias_mes,
        "total_global": total_global, "limite_global": limite_global,
        "pct_global": pct_global, "pct_ritmo": pct_ritmo,
        "proyeccion": proyeccion, "dias_restantes": dias_rest,
        "ritmo_necesario": ritmo_nec,
        "disponibles": max(limite_global - total_global, 0),
        "estado_global": "CRITICO" if pct_global >= crit_g else
                         "ADVERTENCIA" if pct_global >= adv_g else "OK",
        "umbral_adv": adv_g, "umbral_crit": crit_g,
        "dispositivos": dispositivos, "serie_global": serie_global,
        "num_dispositivos": len(dispositivos),
        "num_criticos":    sum(1 for d in dispositivos if d["estado"] == "CRITICO"),
        "num_advertencias":sum(1 for d in dispositivos if d["estado"] == "ADVERTENCIA"),
        "num_ok":          sum(1 for d in dispositivos if d["estado"] == "OK"),
        "actualizado": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
    }
    cache_set(cache_key, resultado)
    return resultado

# ══════════════════════════════════════════════════════════════════════════════
# RUTAS — Auth
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if "user_id" in session:
        return redirect(url_for("index"))
    error = None
    username = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = verificar_usuario(username, password)
        if user:
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            session["rol"]      = user["rol"]
            actualizar_ultimo_login(user["id"])
            return redirect(url_for("index"))
        error = "Usuario o contraseña incorrectos"
    return render_template("login.html",
        error=error, username=username,
        nombre_app=get_config("nombre_app", "Monitor Sigfox"),
        logo_empresa=get_config("logo_empresa", "IotNet"),
        año=date.today().year)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ══════════════════════════════════════════════════════════════════════════════
# RUTAS — Dashboard
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/")
@login_required
def index():
    u = usuario_actual()
    return render_template("index.html",
        usuario=u,
        nombre_app=get_config("nombre_app", "Monitor Sigfox"),
        logo_empresa=get_config("logo_empresa", "IotNet"))


@app.route("/api/datos")
@login_required
def api_datos():
    hoy = date.today()
    year  = request.args.get("year",  str(hoy.year))
    month = request.args.get("month", str(hoy.month))
    force = request.args.get("force", "false") == "true"
    try:
        datos = obtener_datos(year, month, force=force)
        return jsonify({"ok": True, "datos": datos})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/config", methods=["GET"])
@login_required
def api_config_get():
    cfg = cargar_config()
    ecfg = cfg["alertas"]["email"]
    return jsonify({
        "limites": cfg["limites"],
        "alertas": {
            "umbral_advertencia_pct":        cfg["alertas"]["umbral_advertencia_pct"],
            "umbral_critico_pct":             cfg["alertas"]["umbral_critico_pct"],
            "umbral_global_advertencia_pct":  cfg["alertas"].get("umbral_global_advertencia_pct", 75),
            "umbral_global_critico_pct":      cfg["alertas"].get("umbral_global_critico_pct", 90),
            "email": {
                "habilitado":            ecfg["habilitado"],
                "destinatarios":         ecfg["destinatarios"],
                "destinatarios_criticos":ecfg["destinatarios_criticos"],
            },
        },
        "email_config": {
            "smtp_host":    ecfg.get("smtp_host",    "smtp.gmail.com"),
            "smtp_port":    ecfg.get("smtp_port",    587),
            "usuario":      ecfg.get("usuario",      ""),
            "reporte_dia":  cfg.get("email_config", {}).get("reporte_dia",  "fri"),
            "reporte_hora": cfg.get("email_config", {}).get("reporte_hora", "08:00"),
        },
    })


@app.route("/api/config", methods=["POST"])
@admin_required
def api_config_post():
    try:
        body = request.get_json()
        cfg  = cargar_config()
        mapa = {
            "limite_global":     ("limites", "global_mensual", int),
            "limite_diario":     ("limites", "diario_default", int),
            "umbral_adv":        ("alertas", "umbral_advertencia_pct", int),
            "umbral_crit":       ("alertas", "umbral_critico_pct", int),
            "umbral_global_adv": ("alertas", "umbral_global_advertencia_pct", int),
            "umbral_global_crit":("alertas", "umbral_global_critico_pct", int),
        }
        for key, (sec, campo, tipo) in mapa.items():
            if key in body:
                cfg[sec][campo] = tipo(body[key])
        if "destinatarios" in body:
            cfg["alertas"]["email"]["destinatarios"] = body["destinatarios"]
        if "destinatarios_criticos" in body:
            cfg["alertas"]["email"]["destinatarios_criticos"] = body["destinatarios_criticos"]
        if "email_habilitado" in body:
            cfg["alertas"]["email"]["habilitado"] = bool(body["email_habilitado"])
        # SMTP y config de email completa
        smtp_fields = ["smtp_host", "smtp_port", "smtp_usuario", "reporte_dia", "reporte_hora"]
        for f in smtp_fields:
            if f in body:
                key = f.replace("smtp_", "")
                if f == "smtp_host":    cfg["alertas"]["email"]["smtp_host"]  = body[f]
                elif f == "smtp_port":  cfg["alertas"]["email"]["smtp_port"]  = int(body[f])
                elif f == "smtp_usuario":
                    cfg["alertas"]["email"]["usuario"]   = body[f]
                    cfg["alertas"]["email"]["remitente"] = body[f]
                elif f == "reporte_dia":  cfg.setdefault("email_config", {})["reporte_dia"]  = body[f]
                elif f == "reporte_hora": cfg.setdefault("email_config", {})["reporte_hora"] = body[f]
        if body.get("smtp_password"):
            cfg["alertas"]["email"]["password"] = body["smtp_password"]
        # Guardar email_config para el frontend
        if "email_config" not in cfg:
            cfg["email_config"] = {}
        cfg["email_config"]["smtp_host"]    = cfg["alertas"]["email"].get("smtp_host", "smtp.gmail.com")
        cfg["email_config"]["smtp_port"]    = cfg["alertas"]["email"].get("smtp_port", 587)
        cfg["email_config"]["usuario"]      = cfg["alertas"]["email"].get("usuario", "")
        cfg["email_config"]["reporte_dia"]  = cfg.get("email_config", {}).get("reporte_dia", "fri")
        cfg["email_config"]["reporte_hora"] = cfg.get("email_config", {}).get("reporte_hora", "08:00")
        guardar_config_json(cfg)
        # Reprogramar scheduler si cambió día/hora
        if "reporte_dia" in body or "reporte_hora" in body:
            _reprogramar_scheduler(cfg)
        _cache.clear()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/email-prueba", methods=["POST"])
@admin_required
def api_email_prueba():
    try:
        cfg  = cargar_config()
        ecfg = cfg["alertas"]["email"]
        if not ecfg.get("usuario") or not ecfg.get("password"):
            return jsonify({"ok": False, "error": "Configura primero el usuario y contraseña SMTP"}), 400
        html = f"""<html><body style="font-family:Arial,sans-serif">
          <h2 style="color:#1d4ed8">📡 Correo de prueba — Monitor Sigfox</h2>
          <p>Si recibes este correo, la configuración de email es correcta ✅</p>
          <p style="color:#888;font-size:12px;margin-top:16px">
            Servidor: {ecfg['smtp_host']}:{ecfg['smtp_port']}<br>
            Remitente: {ecfg.get('remitente', ecfg.get('usuario',''))}<br>
            Destinatarios: {', '.join(ecfg['destinatarios'])}
          </p>
        </body></html>"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "✅ Prueba de email — Monitor Sigfox"
        msg["From"]    = ecfg.get("remitente", ecfg.get("usuario", ""))
        msg["To"]      = ", ".join(ecfg["destinatarios"])
        msg.attach(MIMEText(html, "html", "utf-8"))
        server = _smtp_conectar(ecfg)
        server.sendmail(msg["From"], ecfg["destinatarios"], msg.as_string())
        server.quit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _reprogramar_scheduler(cfg):
    """Reprograma el job del reporte semanal con el día/hora del config."""
    try:
        ec   = cfg.get("email_config", {})
        dia  = ec.get("reporte_dia",  "fri")
        hora = ec.get("reporte_hora", "08:00")
        h, m = map(int, hora.split(":"))
        scheduler.reschedule_job("reporte_semanal", trigger="cron",
                                  day_of_week=dia, hour=h, minute=m)
        print(f"  [Scheduler] Reprogramado: {dia} {hora}")
    except Exception as e:
        print(f"  [Scheduler] Error al reprogramar: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# RUTAS — Admin
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/admin")
@admin_required
def admin_panel():
    return render_template("admin.html",
        usuario_actual=session.get("username"),
        usuarios=listar_usuarios(),
        usuarios_activos=contar_usuarios_activos(),
        max_usuarios=int(get_config("max_usuarios", 10)),
        nombre_app=get_config("nombre_app", "Monitor Sigfox"),
        logo_empresa=get_config("logo_empresa", "IotNet"),
        msg=request.args.get("msg"),
        msg_tipo=request.args.get("tipo", "ok"),
    )


@app.route("/admin/crear-usuario", methods=["POST"])
@admin_required
def admin_crear_usuario():
    max_u = int(get_config("max_usuarios", 10))
    if contar_usuarios_activos() >= max_u:
        return redirect(url_for("admin_panel", msg="Límite de usuarios alcanzado", tipo="err"))
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    nombre   = request.form.get("nombre", "")
    email    = request.form.get("email", "")
    rol      = request.form.get("rol", "viewer")
    if len(password) < 6:
        return redirect(url_for("admin_panel", msg="La contraseña debe tener al menos 6 caracteres", tipo="err"))
    ok, msg = crear_usuario(username, password, nombre, email, rol)
    return redirect(url_for("admin_panel", msg=msg, tipo="ok" if ok else "err"))


@app.route("/admin/editar-usuario/<int:uid>", methods=["POST"])
@admin_required
def admin_editar_usuario(uid):
    try:
        body = request.get_json()
        actualizar_usuario(
            uid,
            nombre   = body.get("nombre"),
            email    = body.get("email"),
            rol      = body.get("rol"),
            activo   = body.get("activo"),
            password = body.get("password") or None,
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/admin/eliminar-usuario/<int:uid>", methods=["POST"])
@admin_required
def admin_eliminar_usuario(uid):
    if uid == session.get("user_id"):
        return redirect(url_for("admin_panel", msg="No puedes eliminar tu propio usuario", tipo="err"))
    eliminar_usuario(uid)
    return redirect(url_for("admin_panel", msg="Usuario eliminado", tipo="ok"))


@app.route("/admin/config", methods=["POST"])
@admin_required
def admin_config():
    set_config("nombre_app",   request.form.get("nombre_app", "Monitor Sigfox"))
    set_config("logo_empresa", request.form.get("logo_empresa", "IotNet"))
    set_config("max_usuarios", request.form.get("max_usuarios", "10"))
    return redirect(url_for("admin_panel", msg="Configuración guardada", tipo="ok"))

# ══════════════════════════════════════════════════════════════════════════════
# REPORTE CSV
# ══════════════════════════════════════════════════════════════════════════════

def generar_csv(datos):
    """Genera el contenido CSV del reporte de consumos."""
    output = io.StringIO()
    writer = csv.writer(output)

    # Encabezado resumen global
    writer.writerow(["REPORTE DE CONSUMOS SIGFOX"])
    writer.writerow([f"Período: {datos['mes_nombre']} {datos['year']}"])
    writer.writerow([f"Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}"])
    writer.writerow([])

    # Resumen global
    writer.writerow(["RESUMEN GLOBAL"])
    writer.writerow(["Total mensajes consumidos", datos["total_global"]])
    writer.writerow(["Límite mensual del pool",   datos["limite_global"]])
    writer.writerow(["% del pool consumido",      f"{datos['pct_global']}%"])
    writer.writerow(["Proyección fin de mes",      datos["proyeccion"]])
    writer.writerow(["Mensajes disponibles",       datos["disponibles"]])
    writer.writerow(["Estado global",              datos["estado_global"]])
    writer.writerow([])

    # Detalle por dispositivo
    writer.writerow(["DETALLE POR DISPOSITIVO"])
    writer.writerow(["ID Dispositivo", "Mensajes acumulados", "Límite mensual",
                     "% del límite", "Promedio/día", "Límite/día",
                     "Días con datos", "Estado"])
    for d in datos["dispositivos"]:
        writer.writerow([
            d["id"], d["total"], d["limite_mes"],
            f"{d['pct']}%", d["promedio_dia"],
            d["limite_dia"], d["dias_datos"], d["estado"]
        ])

    # Serie diaria global
    writer.writerow([])
    writer.writerow(["CONSUMO DIARIO GLOBAL (todos los dispositivos)"])
    writer.writerow(["Día"] + [str(i+1) for i in range(datos["dias_mes"])])
    writer.writerow(["Mensajes"] + [
        int(v) if v else 0 for v in datos["serie_global"][:datos["dias_mes"]]
    ])

    return output.getvalue()


@app.route("/api/reporte-csv")
@login_required
def api_reporte_csv():
    hoy   = date.today()
    year  = request.args.get("year",  str(hoy.year))
    month = request.args.get("month", str(hoy.month))
    try:
        datos = obtener_datos(year, month)
        csv_content = generar_csv(datos)
        filename = f"reporte_sigfox_{year}_{int(month):02d}.csv"
        return Response(
            csv_content,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL SEMANAL (cada viernes automático)
# ══════════════════════════════════════════════════════════════════════════════

def _smtp_conectar(ecfg):
    if ecfg.get("usar_tls", True):
        server = smtplib.SMTP(ecfg["smtp_host"], ecfg["smtp_port"])
        server.starttls()
    else:
        server = smtplib.SMTP_SSL(ecfg["smtp_host"], ecfg["smtp_port"])
    server.login(ecfg["usuario"], ecfg["password"])
    return server


def enviar_reporte_semanal():
    """Se ejecuta automáticamente cada viernes. Envía resumen + CSV adjunto."""
    try:
        cfg  = cargar_config()
        ecfg = cfg["alertas"]["email"]
        if not ecfg.get("habilitado", False):
            return

        hoy   = date.today()
        year  = str(hoy.year)
        month = str(hoy.month)
        datos = obtener_datos(year, month, force=True)
        csv_content = generar_csv(datos)

        limite_global = datos["limite_global"]
        pct    = datos["pct_global"]
        estado = datos["estado_global"]
        color  = {"OK": "#27ae60", "ADVERTENCIA": "#f39c12", "CRITICO": "#dc2626"}.get(estado, "#333")
        icono  = {"OK": "✅", "ADVERTENCIA": "⚠️", "CRITICO": "🚨"}.get(estado, "")

        html = f"""
        <html><body style="font-family:Arial,sans-serif;max-width:700px">
          <h2 style="color:#1d4ed8">📡 Reporte Semanal de Mensajes Sigfox</h2>
          <p style="color:#64748b">{hoy.strftime('%A %d de %B de %Y')}</p>

          <div style="background:#f8f9fa;border-radius:12px;padding:20px;
                      border-left:5px solid {color};margin:20px 0">
            <div style="font-size:42px;font-weight:900;color:{color}">{pct}%</div>
            <div style="font-weight:700;color:{color}">{icono} {estado}</div>
            <div style="color:#64748b;margin-top:4px">del pool mensual consumido</div>
          </div>

          <table style="width:100%;border-collapse:collapse;font-size:14px">
            <tr style="background:#f1f5f9">
              <td style="padding:10px;font-weight:600">Mensajes consumidos</td>
              <td style="padding:10px;text-align:right;font-weight:800">{datos['total_global']:,}</td>
            </tr>
            <tr>
              <td style="padding:10px">Límite mensual</td>
              <td style="padding:10px;text-align:right">{limite_global:,}</td>
            </tr>
            <tr style="background:#f1f5f9">
              <td style="padding:10px">Proyección fin de mes</td>
              <td style="padding:10px;text-align:right">{datos['proyeccion']:,}</td>
            </tr>
            <tr>
              <td style="padding:10px">Disponibles</td>
              <td style="padding:10px;text-align:right;color:#27ae60;font-weight:700">
                {datos['disponibles']:,}
              </td>
            </tr>
            <tr style="background:#f1f5f9">
              <td style="padding:10px">Dispositivos 🔴 Crítico</td>
              <td style="padding:10px;text-align:right;color:#dc2626">{datos['num_criticos']}</td>
            </tr>
            <tr>
              <td style="padding:10px">Dispositivos 🟡 Advertencia</td>
              <td style="padding:10px;text-align:right;color:#d97706">{datos['num_advertencias']}</td>
            </tr>
          </table>

          <p style="color:#94a3b8;font-size:11px;margin-top:24px">
            Reporte generado automáticamente cada viernes — Monitor Sigfox IotNet<br>
            Se adjunta el detalle completo en CSV.
          </p>
        </body></html>"""

        msg = MIMEMultipart("mixed")
        msg["Subject"] = f"📊 Reporte Semanal Sigfox — {pct}% del pool — {datos['mes_nombre']} {year}"
        msg["From"]    = ecfg["remitente"]
        msg["To"]      = ", ".join(ecfg["destinatarios"])
        msg.attach(MIMEText(html, "html", "utf-8"))

        # Adjuntar CSV
        adjunto = MIMEBase("application", "octet-stream")
        adjunto.set_payload(csv_content.encode("utf-8"))
        encoders.encode_base64(adjunto)
        adjunto.add_header("Content-Disposition",
                           f"attachment; filename=reporte_sigfox_{year}_{int(month):02d}.csv")
        msg.attach(adjunto)

        server = _smtp_conectar(ecfg)
        server.sendmail(ecfg["remitente"], ecfg["destinatarios"], msg.as_string())
        server.quit()
        print(f"  [Scheduler] Reporte semanal enviado a {ecfg['destinatarios']}")

    except Exception as e:
        print(f"  [Scheduler] Error al enviar reporte semanal: {e}")


# ── Iniciar scheduler ─────────────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone="America/Mexico_City")
scheduler.add_job(
    enviar_reporte_semanal,
    trigger="cron",
    day_of_week="fri",       # cada viernes
    hour=8,
    minute=0,
    id="reporte_semanal",
    replace_existing=True,
)
scheduler.start()

# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    print("\n  📡 Monitor Sigfox iniciando...")
    print("  http://localhost:5000")
    print("  Usuario admin: admin / admin123  ← cámbialo en el panel\n")
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
