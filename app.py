#!/usr/bin/env python3
# coding: utf-8
"""
Monitor Sigfox — Web App con autenticación
Ejecutar:  python3 app.py
"""

import json
import os
import calendar
import time
from datetime import date, datetime
from functools import wraps

import pandas as pd
import requests
from flask import (Flask, jsonify, render_template, request,
                   redirect, url_for, session, flash)
from requests.auth import HTTPBasicAuth

from database import (init_db, verificar_usuario, get_usuario, get_config,
                      set_config, actualizar_ultimo_login, listar_usuarios,
                      crear_usuario, actualizar_usuario, eliminar_usuario,
                      contar_usuarios_activos)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sigfox-monitor-secret-2024-iotnet")

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
    return jsonify({
        "limites": cfg["limites"],
        "alertas": {
            "umbral_advertencia_pct":        cfg["alertas"]["umbral_advertencia_pct"],
            "umbral_critico_pct":             cfg["alertas"]["umbral_critico_pct"],
            "umbral_global_advertencia_pct":  cfg["alertas"].get("umbral_global_advertencia_pct", 75),
            "umbral_global_critico_pct":      cfg["alertas"].get("umbral_global_critico_pct", 90),
            "email": {
                "habilitado":            cfg["alertas"]["email"]["habilitado"],
                "destinatarios":         cfg["alertas"]["email"]["destinatarios"],
                "destinatarios_criticos":cfg["alertas"]["email"]["destinatarios_criticos"],
            },
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
        if "email_habilitado" in body:
            cfg["alertas"]["email"]["habilitado"] = bool(body["email_habilitado"])
        guardar_config_json(cfg)
        _cache.clear()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

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
if __name__ == "__main__":
    init_db()
    print("\n  📡 Monitor Sigfox iniciando...")
    print("  http://localhost:5000")
    print("  Usuario admin: admin / admin123  ← cámbialo en el panel\n")
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
