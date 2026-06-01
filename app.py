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

import requests
from flask import (Flask, jsonify, render_template, request,
                   redirect, url_for, session, flash, Response)
from requests.auth import HTTPBasicAuth
from apscheduler.schedulers.background import BackgroundScheduler

from database import (init_db, verificar_usuario, get_usuario, get_config,
                      set_config, actualizar_ultimo_login, listar_usuarios,
                      crear_usuario, actualizar_usuario, eliminar_usuario,
                      contar_usuarios_activos, get_ids_dispositivos,
                      listar_dispositivos, agregar_dispositivo,
                      eliminar_dispositivo, toggle_dispositivo,
                      importar_dispositivos_csv, contar_dispositivos,
                      # Proyectos
                      listar_proyectos, get_proyecto, crear_proyecto,
                      actualizar_proyecto, eliminar_proyecto, contar_proyectos,
                      get_dispositivos_proyecto, get_dispositivos_proyecto_detalle,
                      asignar_dispositivos_proyecto, agregar_dispositivo_proyecto,
                      quitar_dispositivo_proyecto, get_proyectos_de_dispositivo,
                      get_proyectos_de_usuario, get_proyectos_visibles,
                      asignar_usuarios_proyecto, get_usuarios_de_proyecto)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sigfox-monitor-secret-2024-iotnet")

@app.errorhandler(Exception)
def handle_exception(e):
    import traceback
    print(f"  [ERROR] Excepción no controlada: {e}")
    traceback.print_exc()
    return jsonify({"ok": False, "error": str(e), "type": type(e).__name__}), 500

# Inicializar DB siempre al arrancar (con o sin gunicorn)
_db_url = os.environ.get("DATABASE_URL", "")
if not _db_url:
    print("  [ERROR] DATABASE_URL no configurada.")
else:
    try:
        init_db()
        print("  [DB] Base de datos inicializada correctamente.")
    except Exception as _e:
        print(f"  [ERROR] init_db falló: {_e}")
        import traceback; traceback.print_exc()

# ── Cache en memoria + disco ──────────────────────────────────────────────────
_cache   = {}
CACHE_TTL = 300  # 5 min en memoria
DATA_DIR  = os.path.join(SCRIPT_DIR, "data")

def _cache_file(key):
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, f"cache_{key}.json")

def cache_get(key):
    # 1. Memoria (más rápido)
    e = _cache.get(key)
    if e and (time.time() - e["ts"]) < CACHE_TTL:
        return e["data"]
    # 2. Disco (persiste entre reinicios)
    try:
        path = _cache_file(key)
        if os.path.exists(path):
            with open(path, "r") as f:
                stored = json.load(f)
            _cache[key] = {"ts": stored["ts"], "data": stored["data"]}
            return stored["data"]
    except Exception:
        pass
    return None

def cache_set(key, data):
    ts = time.time()
    _cache[key] = {"ts": ts, "data": data}
    try:
        with open(_cache_file(key), "w") as f:
            json.dump({"ts": ts, "data": data}, f)
    except Exception:
        pass

def cache_clear_all():
    """Limpia caché en memoria y en disco."""
    import glob
    _cache.clear()
    try:
        for f in glob.glob(os.path.join(DATA_DIR, "cache_*.json")):
            os.remove(f)
    except Exception:
        pass

def cache_get_stale(key):
    """Devuelve datos aunque estén viejos (para mostrar mientras refresca)."""
    e = _cache.get(key)
    if e:
        return e["data"]
    try:
        path = _cache_file(key)
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)["data"]
    except Exception:
        pass
    return None

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
CONFIG_DEFAULT = {
    "sigfox": {
        "login":    os.environ.get("SIGFOX_LOGIN",    ""),
        "password": os.environ.get("SIGFOX_PASSWORD", ""),
    },
    "limites": {
        "diario_default":  140,
        "global_mensual":  5000000,
        "por_dispositivo": {},
    },
    "alertas": {
        "umbral_advertencia_pct":       50,
        "umbral_critico_pct":           95,
        "umbral_global_advertencia_pct":75,
        "umbral_global_critico_pct":    90,
        "email": {
            "habilitado":              False,
            "smtp_host":               "smtp.gmail.com",
            "smtp_port":               587,
            "usar_tls":                True,
            "usuario":                 "",
            "password":                "",
            "remitente":               "",
            "destinatarios":           [],
            "destinatarios_criticos":  [],
        },
    },
    "email_config": {
        "reporte_dia":  "fri",
        "reporte_hora": "08:00",
    },
}

def cargar_config():
    if not os.path.exists(CONFIG_PATH):
        # En Railway (o primera ejecución) crea config desde vars de entorno
        cfg = json.loads(json.dumps(CONFIG_DEFAULT))  # deep-copy
        cfg["sigfox"]["login"]    = os.environ.get("SIGFOX_LOGIN",    "")
        cfg["sigfox"]["password"] = os.environ.get("SIGFOX_PASSWORD", "")
        guardar_config_json(cfg)
        return cfg
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Rellena claves que podrían faltar en versiones antiguas del archivo
    if "sigfox" not in cfg:
        cfg["sigfox"] = {"login": os.environ.get("SIGFOX_LOGIN", ""),
                         "password": os.environ.get("SIGFOX_PASSWORD", "")}
    else:
        if not cfg["sigfox"].get("login"):
            cfg["sigfox"]["login"]    = os.environ.get("SIGFOX_LOGIN",    "")
        if not cfg["sigfox"].get("password"):
            cfg["sigfox"]["password"] = os.environ.get("SIGFOX_PASSWORD", "")
    cfg.setdefault("email_config", {"reporte_dia": "fri", "reporte_hora": "08:00"})
    if "alertas" not in cfg:
        cfg["alertas"] = CONFIG_DEFAULT["alertas"]
    cfg["alertas"].setdefault("email", CONFIG_DEFAULT["alertas"]["email"])
    cfg["alertas"]["email"].setdefault("destinatarios",          [])
    cfg["alertas"]["email"].setdefault("destinatarios_criticos", [])
    cfg["alertas"]["email"].setdefault("smtp_host",  "smtp.gmail.com")
    cfg["alertas"]["email"].setdefault("smtp_port",  587)
    cfg["alertas"]["email"].setdefault("usar_tls",   True)
    cfg["alertas"]["email"].setdefault("usuario",    "")
    cfg["alertas"]["email"].setdefault("password",   "")
    cfg["alertas"]["email"].setdefault("remitente",  "")
    cfg.setdefault("limites", CONFIG_DEFAULT["limites"])
    cfg["limites"].setdefault("por_dispositivo", {})
    return cfg

def guardar_config_json(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

# ── Sigfox API ────────────────────────────────────────────────────────────────
def consultar_consumo_api(login, password, device_id, year, month):
    url = f"https://api.sigfox.com/v2/devices/{device_id}/consumption/{year}/{month}"
    try:
        r = requests.get(url, auth=HTTPBasicAuth(login, password), timeout=8)
        if r.status_code == 200:
            return r.json().get("consumption", {}).get("consumptions", [])
    except Exception as e:
        print(f"  [ERR] {device_id}: {e}")
    return None

def _consultar_device(args):
    """Consulta un dispositivo — ejecutable en ThreadPool."""
    login, password, device_id, year, month, dias_mes = args
    try:
        consumptions = consultar_consumo_api(login, password, device_id, year, month)
        if consumptions is None:
            return None
        diarios = []
        for d in range(dias_mes):
            try:
                v = consumptions[d]["frameCount"] if d < len(consumptions) else None
            except (IndexError, KeyError, TypeError):
                v = None
            diarios.append(v)
        return {"id": str(device_id), "diarios": diarios,
                "total": sum(v for v in diarios if v is not None)}
    except Exception as e:
        print(f"  [ERR] Dispositivo {device_id}: {e}")
        return None


def obtener_datos(year, month, proyecto_id=None, force=False):
    from concurrent.futures import ThreadPoolExecutor
    cache_key = f"{proyecto_id}_{year}_{month}" if proyecto_id else f"all_{year}_{month}"
    if not force:
        cached = cache_get(cache_key)
        if cached:
            return cached

    cfg = cargar_config()
    ids = (get_dispositivos_proyecto(proyecto_id)
           if proyecto_id else get_ids_dispositivos())

    # Límite global: del proyecto si existe, sino del config global
    if proyecto_id:
        p = get_proyecto(proyecto_id)
        limite_global = p["limite_global"] if p else cfg["limites"]["global_mensual"]
    else:
        limite_global = cfg["limites"]["global_mensual"]

    dias_mes = calendar.monthrange(int(year), int(month))[1]
    hoy = date.today()
    dia_corte = hoy.day if (int(year) == hoy.year and int(month) == hoy.month) else dias_mes

    # Consultar todos los dispositivos en paralelo (máx 10 hilos)
    login   = cfg["sigfox"]["login"]
    password = cfg["sigfox"]["password"]
    args_list = [(login, password, dev, year, month, dias_mes) for dev in ids]
    registros = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        for resultado in executor.map(_consultar_device, args_list):
            if resultado is not None:
                registros.append(resultado)

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
@app.route("/health")
def health():
    """Diagnóstico público — muestra estado de DB y vars de entorno."""
    import traceback
    info = {
        "DATABASE_URL_set": bool(os.environ.get("DATABASE_URL")),
        "SIGFOX_LOGIN_set": bool(os.environ.get("SIGFOX_LOGIN")),
        "db_ok": False,
        "tables": [],
        "error": None,
    }
    try:
        from database import get_db, _get_url
        info["db_url_prefix"] = _get_url()[:40] + "..." if _get_url() else "VACÍA"
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
        """)
        info["tables"] = [r["table_name"] for r in cur.fetchall()]
        info["db_ok"] = True
        cur.close(); conn.close()
    except Exception as e:
        info["error"] = str(e)
        info["traceback"] = traceback.format_exc()
    return jsonify(info)


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
    try:
        nombre_app  = get_config("nombre_app",  "Monitor Sigfox")
        logo_empresa = get_config("logo_empresa", "IotNet")
    except Exception:
        nombre_app  = "Monitor Sigfox"
        logo_empresa = "IotNet"
    return render_template("login.html",
        error=error, username=username,
        nombre_app=nombre_app, logo_empresa=logo_empresa,
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
    proyectos = get_proyectos_visibles(u["id"], u["rol"])
    return render_template("index.html",
        usuario=u,
        proyectos=proyectos,
        nombre_app=get_config("nombre_app", "Monitor Sigfox"),
        logo_empresa=get_config("logo_empresa", "IotNet"))


_refreshing = set()  # evita consultas paralelas

def _refresh_background(year, month, proyecto_id=None):
    key = f"{proyecto_id}_{year}_{month}"
    if key in _refreshing:
        return
    _refreshing.add(key)
    try:
        obtener_datos(year, month, proyecto_id=proyecto_id, force=True)
    except Exception as e:
        print(f"  [BG] Error al refrescar {key}: {e}")
    finally:
        _refreshing.discard(key)


def _make_placeholder(year, month):
    return {
        "year": year, "month": month,
        "mes_nombre": calendar.month_name[int(month)],
        "dia_corte": date.today().day,
        "dias_mes": calendar.monthrange(int(year), int(month))[1],
        "total_global": 0, "limite_global": 5000000,
        "pct_global": 0, "pct_ritmo": 0, "proyeccion": 0,
        "dias_restantes": 0, "ritmo_necesario": 0, "disponibles": 5000000,
        "estado_global": "OK", "umbral_adv": 75, "umbral_crit": 90,
        "dispositivos": [], "serie_global": [],
        "num_dispositivos": 0, "num_criticos": 0,
        "num_advertencias": 0, "num_ok": 0,
        "actualizado": "Cargando...", "actualizando": True,
    }


@app.route("/api/proyectos")
@login_required
def api_proyectos():
    u = usuario_actual()
    proyectos = get_proyectos_visibles(u["id"], u["rol"])
    return jsonify({"ok": True, "proyectos": proyectos})


@app.route("/api/datos")
@login_required
def api_datos():
    import threading
    hoy         = date.today()
    year        = request.args.get("year",  str(hoy.year))
    month       = request.args.get("month", str(hoy.month))
    force       = request.args.get("force", "false") == "true"
    proyecto_id = request.args.get("proyecto_id")
    if proyecto_id:
        proyecto_id = int(proyecto_id)
        # Verificar acceso
        u = usuario_actual()
        visibles = [p["id"] for p in get_proyectos_visibles(u["id"], u["rol"])]
        if proyecto_id not in visibles:
            return jsonify({"ok": False, "error": "Sin acceso a este proyecto"}), 403

    key   = f"{proyecto_id}_{year}_{month}"
    stale = cache_get_stale(key)
    fresh = cache_get(key)

    if force:
        threading.Thread(target=_refresh_background,
                         args=(year, month), kwargs={"proyecto_id": proyecto_id},
                         daemon=True).start()
        if stale:
            stale["actualizando"] = True
            return jsonify({"ok": True, "datos": stale, "desde_cache": True, "actualizando": True})
        return jsonify({"ok": True, "datos": _make_placeholder(year, month),
                        "desde_cache": False, "actualizando": True})

    if fresh:
        return jsonify({"ok": True, "datos": fresh, "desde_cache": True})

    if stale:
        threading.Thread(target=_refresh_background,
                         args=(year, month), kwargs={"proyecto_id": proyecto_id},
                         daemon=True).start()
        stale["actualizando"] = True
        return jsonify({"ok": True, "datos": stale, "desde_cache": True, "actualizando": True})

    threading.Thread(target=_refresh_background,
                     args=(year, month), kwargs={"proyecto_id": proyecto_id},
                     daemon=True).start()
    return jsonify({"ok": True, "datos": _make_placeholder(year, month),
                    "desde_cache": False, "actualizando": True})


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
                "habilitado":            ecfg.get("habilitado", False),
                "destinatarios":         ecfg.get("destinatarios", []),
                "destinatarios_criticos":ecfg.get("destinatarios_criticos", []),
                "smtp_host":             ecfg.get("smtp_host",  "smtp.gmail.com"),
                "smtp_port":             ecfg.get("smtp_port",  587),
                "usuario":               ecfg.get("usuario",   ""),
                "password":              ecfg.get("password",  ""),
                "usar_tls":              ecfg.get("usar_tls",  True),
            },
        },
        "email_config": {
            "reporte_dia":  cfg.get("email_config", {}).get("reporte_dia",  "fri"),
            "reporte_hora": cfg.get("email_config", {}).get("reporte_hora", "08:00"),
        },
        "resend_api_key_set": bool(os.environ.get("RESEND_API_KEY")
                                   or ecfg.get("resend_api_key")),
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
        # Campos SMTP (nombres que envía el frontend)
        if "smtp_host" in body:
            cfg["alertas"]["email"]["smtp_host"] = body["smtp_host"]
        if "smtp_port" in body:
            cfg["alertas"]["email"]["smtp_port"] = int(body["smtp_port"] or 587)
        if "smtp_user" in body:
            cfg["alertas"]["email"]["usuario"]   = body["smtp_user"]
            cfg["alertas"]["email"]["remitente"] = body["smtp_user"]
        if "smtp_pass" in body and body["smtp_pass"]:
            cfg["alertas"]["email"]["password"] = body["smtp_pass"]
        if "smtp_tls" in body:
            cfg["alertas"]["email"]["usar_tls"] = bool(body["smtp_tls"])
        if body.get("resend_api_key"):
            cfg["alertas"]["email"]["resend_api_key"] = body["resend_api_key"]
        if "reporte_dia" in body:
            cfg.setdefault("email_config", {})["reporte_dia"]  = body["reporte_dia"]
        if "reporte_hora" in body:
            cfg.setdefault("email_config", {})["reporte_hora"] = body["reporte_hora"]
        guardar_config_json(cfg)
        # Reprogramar scheduler si cambió día/hora
        if "reporte_dia" in body or "reporte_hora" in body:
            _reprogramar_scheduler(cfg)
        cache_clear_all()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/test-email", methods=["POST"])
@app.route("/api/email-prueba", methods=["POST"])
@admin_required
def api_email_prueba():
    try:
        cfg  = cargar_config()
        ecfg = cfg["alertas"]["email"]
        resend_key = os.environ.get("RESEND_API_KEY") or ecfg.get("resend_api_key", "")
        if not resend_key and (not ecfg.get("usuario") or not ecfg.get("password")):
            return jsonify({"ok": False,
                            "error": "Configura SMTP (usuario+contraseña) o añade RESEND_API_KEY"}), 400
        remitente    = _remitente(ecfg)
        destinatarios = ecfg.get("destinatarios", [])
        metodo = "Resend API" if resend_key else f"SMTP {ecfg.get('smtp_host','?')}:{ecfg.get('smtp_port','?')}"
        html = f"""<html><body style="font-family:Arial,sans-serif">
          <h2 style="color:#1d4ed8">📡 Correo de prueba — Monitor Sigfox</h2>
          <p>Si recibes este correo, la configuración de email es correcta ✅</p>
          <p style="color:#888;font-size:12px;margin-top:16px">
            Método: {metodo}<br>
            Remitente: {remitente}<br>
            Destinatarios: {', '.join(destinatarios)}
          </p>
        </body></html>"""
        _enviar_email(ecfg, remitente, destinatarios,
                      "✅ Prueba de email — Monitor Sigfox", html)
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
        dispositivos=listar_dispositivos(solo_activos=False),
        msg=request.args.get("msg"),
        msg_tipo=request.args.get("tipo", "ok"),
    )


# ── Dispositivos API ──────────────────────────────────────────────────────────

@app.route("/admin/dispositivos/upload-csv", methods=["POST"])
@admin_required
def admin_upload_csv():
    try:
        f = request.files.get("archivo")
        if not f:
            return jsonify({"ok": False, "error": "No se recibió archivo"}), 400
        texto = f.read().decode("utf-8", errors="ignore")
        reemplazar = request.form.get("reemplazar", "false") == "true"
        ins, dup, err = importar_dispositivos_csv(texto, reemplazar=reemplazar)
        cache_clear_all()
        return jsonify({"ok": True, "insertados": ins, "duplicados": dup, "errores": err})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/dispositivos/agregar", methods=["POST"])
@admin_required
def admin_agregar_dispositivo():
    try:
        body = request.get_json()
        device_id = (body.get("device_id") or "").strip()
        nombre    = (body.get("nombre")    or "").strip()
        if not device_id:
            return jsonify({"ok": False, "error": "ID requerido"}), 400
        ok, msg = agregar_dispositivo(device_id, nombre)
        if ok:
            cache_clear_all()
        return jsonify({"ok": ok, "msg": msg})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/dispositivos/eliminar/<device_id>", methods=["POST"])
@admin_required
def admin_eliminar_dispositivo(device_id):
    try:
        eliminar_dispositivo(device_id)
        cache_clear_all()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/dispositivos/toggle/<device_id>", methods=["POST"])
@admin_required
def admin_toggle_dispositivo(device_id):
    try:
        body   = request.get_json()
        activo = int(body.get("activo", 1))
        toggle_dispositivo(device_id, activo)
        cache_clear_all()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/clear-cache", methods=["POST"])
@admin_required
def api_clear_cache():
    """Limpia la caché en memoria y en disco."""
    _cache.clear()
    try:
        import glob
        for f in glob.glob(os.path.join(DATA_DIR, "cache_*.json")):
            os.remove(f)
    except Exception:
        pass
    return jsonify({"ok": True})


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
# RUTAS — Proyectos (admin)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/admin/proyectos/crear", methods=["POST"])
@admin_required
def admin_crear_proyecto():
    try:
        body = request.get_json()
        ok, result = crear_proyecto(
            nombre        = body.get("nombre", "").strip(),
            descripcion   = body.get("descripcion", ""),
            cliente       = body.get("cliente", ""),
            limite_global = int(body.get("limite_global", 5000000)),
        )
        if ok:
            return jsonify({"ok": True, "id": result})
        return jsonify({"ok": False, "error": result}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/proyectos/<int:pid>/editar", methods=["POST"])
@admin_required
def admin_editar_proyecto(pid):
    try:
        body = request.get_json()
        actualizar_proyecto(
            pid,
            nombre        = body.get("nombre"),
            descripcion   = body.get("descripcion"),
            cliente       = body.get("cliente"),
            limite_global = body.get("limite_global"),
            activo        = body.get("activo"),
        )
        cache_clear_all()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/proyectos/<int:pid>/eliminar", methods=["POST"])
@admin_required
def admin_eliminar_proyecto(pid):
    try:
        eliminar_proyecto(pid)
        cache_clear_all()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/proyectos/<int:pid>/dispositivos", methods=["GET"])
@admin_required
def admin_get_dispositivos_proyecto(pid):
    try:
        asignados = get_dispositivos_proyecto_detalle(pid)
        todos     = listar_dispositivos(solo_activos=False)
        asig_ids  = {d["device_id"] for d in asignados}
        return jsonify({
            "ok": True,
            "asignados": [d["device_id"] for d in asignados],
            "todos": [{"device_id": d["device_id"], "nombre": d.get("nombre", ""),
                       "asignado": d["device_id"] in asig_ids} for d in todos],
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/proyectos/<int:pid>/dispositivos", methods=["POST"])
@admin_required
def admin_set_dispositivos_proyecto(pid):
    try:
        body = request.get_json()
        asignar_dispositivos_proyecto(pid, body.get("device_ids", []))
        cache_clear_all()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/proyectos/<int:pid>/usuarios", methods=["GET"])
@admin_required
def admin_get_usuarios_proyecto(pid):
    try:
        asignados = get_usuarios_de_proyecto(pid)
        todos     = listar_usuarios()
        asig_ids  = {u["id"] for u in asignados}
        return jsonify({
            "ok": True,
            "todos": [{"id": u["id"], "username": u["username"],
                       "nombre": u.get("nombre", ""), "rol": u["rol"],
                       "asignado": u["id"] in asig_ids} for u in todos
                      if u["rol"] != "admin"],   # admins ven todo, no se asignan
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/proyectos/<int:pid>/usuarios", methods=["POST"])
@admin_required
def admin_set_usuarios_proyecto(pid):
    try:
        body = request.get_json()
        asignar_usuarios_proyecto(pid, body.get("user_ids", []))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/proyectos", methods=["GET"])
@admin_required
def admin_proyectos_json():
    try:
        proyectos = listar_proyectos(solo_activos=False)
        for p in proyectos:
            p["num_dispositivos"] = len(get_dispositivos_proyecto(p["id"]))
            p["num_usuarios"]     = len(get_usuarios_de_proyecto(p["id"]))
        return jsonify({"ok": True, "proyectos": proyectos})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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

def _enviar_via_resend(api_key, remitente, destinatarios, asunto, html):
    """Envía email usando la API HTTP de Resend (resend.com). No usa SMTP."""
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={"from": remitente,
              "to": destinatarios if isinstance(destinatarios, list) else [destinatarios],
              "subject": asunto,
              "html": html},
        timeout=20,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Resend API error {resp.status_code}: {resp.text}")
    return resp.json()


def _enviar_email(ecfg, remitente, destinatarios, asunto, html, adjunto_csv=None):
    """
    Envía email eligiendo automáticamente el método:
      1. Resend API  (si está configurado RESEND_API_KEY en env o en config)
      2. SMTP        (fallback)
    """
    resend_key = (os.environ.get("RESEND_API_KEY")
                  or ecfg.get("resend_api_key", ""))

    if resend_key:
        # ── Resend (HTTP, funciona en Render free) ────────────────────────────
        print("  [Email] Enviando via Resend API...")
        if adjunto_csv:
            import base64
            # Resend soporta adjuntos en base64
            requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_key}",
                         "Content-Type": "application/json"},
                json={
                    "from": remitente,
                    "to": destinatarios if isinstance(destinatarios, list) else [destinatarios],
                    "subject": asunto,
                    "html": html,
                    "attachments": [{
                        "filename": adjunto_csv["filename"],
                        "content": base64.b64encode(
                            adjunto_csv["content"].encode("utf-8")).decode(),
                    }],
                },
                timeout=20,
            ).raise_for_status()
        else:
            _enviar_via_resend(resend_key, remitente, destinatarios, asunto, html)
    else:
        # ── SMTP clásico ──────────────────────────────────────────────────────
        print("  [Email] Enviando via SMTP...")
        host = ecfg.get("smtp_host", "smtp.gmail.com")
        port = int(ecfg.get("smtp_port", 587))
        user = ecfg.get("usuario", "")
        pw   = ecfg.get("password", "")
        if not host or not user or not pw:
            raise ValueError("Configuración SMTP incompleta (host/usuario/password). "
                             "Configura SMTP o añade RESEND_API_KEY como variable de entorno.")

        import socket
        try:
            # Forzar IPv4 (Render free no soporta IPv6)
            infos = socket.getaddrinfo(host, None, socket.AF_INET)
            ip4 = infos[0][4][0]
        except Exception:
            ip4 = host

        if ecfg.get("usar_tls", True):
            server = smtplib.SMTP(ip4, port, timeout=20)
            server.ehlo(host); server.starttls(); server.ehlo(host)
        else:
            server = smtplib.SMTP_SSL(ip4, port, timeout=20)
        server.login(user, pw)

        if adjunto_csv:
            msg = MIMEMultipart("mixed")
            msg["Subject"] = asunto
            msg["From"]    = remitente
            msg["To"]      = ", ".join(destinatarios if isinstance(destinatarios, list) else [destinatarios])
            msg.attach(MIMEText(html, "html", "utf-8"))
            part = MIMEBase("application", "octet-stream")
            part.set_payload(adjunto_csv["content"].encode("utf-8"))
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{adjunto_csv["filename"]}"')
            msg.attach(part)
            server.sendmail(remitente, destinatarios, msg.as_string())
        else:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = asunto
            msg["From"]    = remitente
            msg["To"]      = ", ".join(destinatarios if isinstance(destinatarios, list) else [destinatarios])
            msg.attach(MIMEText(html, "html", "utf-8"))
            server.sendmail(remitente, destinatarios, msg.as_string())
        server.quit()


def _remitente(ecfg):
    """Devuelve el correo remitente, con fallback al usuario."""
    return ecfg.get("remitente") or ecfg.get("usuario") or ""


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

        remitente = _remitente(ecfg)
        destinatarios = ecfg.get("destinatarios", [])
        if not destinatarios:
            print("  [Scheduler] Sin destinatarios configurados, omitiendo reporte.")
            return

        _enviar_email(
            ecfg, remitente, destinatarios,
            f"📊 Reporte Semanal Sigfox — {pct}% del pool — {datos['mes_nombre']} {year}",
            html,
            adjunto_csv={
                "filename": f"reporte_sigfox_{year}_{int(month):02d}.csv",
                "content":  csv_content,
            }
        )
        print(f"  [Scheduler] Reporte semanal enviado a {destinatarios}")

    except Exception as e:
        print(f"  [Scheduler] Error al enviar reporte semanal: {e}")


# ── Alertas diarias ───────────────────────────────────────────────────────────
# Evita enviar la misma alerta más de una vez al día
_ultima_alerta = {"global": None, "dispositivos": {}}


def verificar_y_enviar_alertas():
    """Se ejecuta 3 veces al día. Envía email si se supera un umbral."""
    try:
        cfg  = cargar_config()
        ecfg = cfg["alertas"]["email"]
        if not ecfg.get("habilitado", False):
            return

        destinatarios = ecfg.get("destinatarios", [])
        dest_criticos = ecfg.get("destinatarios_criticos", []) or destinatarios
        if not destinatarios:
            return

        hoy   = date.today().isoformat()
        year  = str(date.today().year)
        month = str(date.today().month)

        # Usa caché si existe, no fuerza nueva consulta
        datos = cache_get_stale(f"{year}_{month}")
        if not datos:
            return   # Sin datos aún, no hay nada que alertar

        pct_global    = datos.get("pct_global", 0)
        umbral_adv_g  = cfg["alertas"].get("umbral_global_advertencia_pct", 75)
        umbral_crit_g = cfg["alertas"].get("umbral_global_critico_pct", 90)
        umbral_adv_d  = cfg["alertas"].get("umbral_advertencia_pct", 50)
        umbral_crit_d = cfg["alertas"].get("umbral_critico_pct", 95)

        remitente = _remitente(ecfg)

        def _enviar(asunto, html_body, dests):
            _enviar_email(ecfg, remitente, dests, asunto, html_body)
            print(f"  [Alertas] Enviado: {asunto}")

        # ── Alerta global ──────────────────────────────────────────────────────
        nivel_global = None
        if pct_global >= umbral_crit_g:
            nivel_global = "CRITICO"
        elif pct_global >= umbral_adv_g:
            nivel_global = "ADVERTENCIA"

        if nivel_global:
            clave = f"{hoy}_{nivel_global}"
            if _ultima_alerta["global"] != clave:
                _ultima_alerta["global"] = clave
                color = "#dc2626" if nivel_global == "CRITICO" else "#d97706"
                icono = "🚨" if nivel_global == "CRITICO" else "⚠️"
                dests_envio = dest_criticos if nivel_global == "CRITICO" else destinatarios
                html = f"""<html><body style="font-family:Arial,sans-serif;max-width:650px">
                  <h2 style="color:{color}">{icono} Alerta de Consumo Sigfox — {nivel_global}</h2>
                  <div style="background:#f8fafc;border-left:5px solid {color};
                              border-radius:8px;padding:20px;margin:16px 0">
                    <div style="font-size:48px;font-weight:900;color:{color}">{pct_global}%</div>
                    <div style="font-weight:700;color:{color}">del pool mensual consumido</div>
                    <div style="color:#64748b;margin-top:4px">
                      {datos['total_global']:,} de {datos['limite_global']:,} mensajes
                    </div>
                  </div>
                  <table style="width:100%;border-collapse:collapse;font-size:14px">
                    <tr style="background:#f1f5f9">
                      <td style="padding:10px;font-weight:600">Proyección fin de mes</td>
                      <td style="padding:10px;text-align:right;font-weight:800">
                        {datos.get('proyeccion',0):,}
                      </td>
                    </tr>
                    <tr>
                      <td style="padding:10px">Disponibles</td>
                      <td style="padding:10px;text-align:right;color:#16a34a;font-weight:700">
                        {datos.get('disponibles',0):,}
                      </td>
                    </tr>
                    <tr style="background:#f1f5f9">
                      <td style="padding:10px">Dispositivos críticos</td>
                      <td style="padding:10px;text-align:right;color:#dc2626">
                        {datos.get('num_criticos',0)}
                      </td>
                    </tr>
                  </table>
                  <p style="color:#94a3b8;font-size:11px;margin-top:20px">
                    Monitor Sigfox — {datos['mes_nombre']} {year}
                  </p>
                </body></html>"""
                try:
                    _enviar(
                        f"{icono} Alerta {nivel_global}: pool Sigfox al {pct_global}% — {datos['mes_nombre']} {year}",
                        html, dests_envio
                    )
                except Exception as e:
                    print(f"  [Alertas] Error alerta global: {e}")

        # ── Alertas por dispositivo ────────────────────────────────────────────
        for dev in datos.get("dispositivos", []):
            if dev["total"] == 0:
                continue
            nivel_dev = None
            if dev["pct"] >= umbral_crit_d:
                nivel_dev = "CRITICO"
            elif dev["pct"] >= umbral_adv_d:
                nivel_dev = "ADVERTENCIA"
            if not nivel_dev:
                continue

            clave_dev = f"{hoy}_{dev['id']}_{nivel_dev}"
            if _ultima_alerta["dispositivos"].get(dev["id"]) == clave_dev:
                continue
            _ultima_alerta["dispositivos"][dev["id"]] = clave_dev

            color = "#dc2626" if nivel_dev == "CRITICO" else "#d97706"
            icono = "🚨" if nivel_dev == "CRITICO" else "⚠️"
            dests_envio = dest_criticos if nivel_dev == "CRITICO" else destinatarios
            html_dev = f"""<html><body style="font-family:Arial,sans-serif;max-width:650px">
              <h2 style="color:{color}">{icono} Dispositivo en {nivel_dev}: {dev['id']}</h2>
              <div style="background:#f8fafc;border-left:5px solid {color};
                          border-radius:8px;padding:20px;margin:16px 0">
                <div style="font-size:36px;font-weight:900;color:{color}">{dev['pct']}%</div>
                <div style="font-weight:700">del límite mensual consumido</div>
                <div style="color:#64748b;margin-top:4px;font-family:monospace">ID: {dev['id']}</div>
              </div>
              <table style="width:100%;border-collapse:collapse;font-size:14px">
                <tr style="background:#f1f5f9">
                  <td style="padding:10px;font-weight:600">Mensajes acumulados</td>
                  <td style="padding:10px;text-align:right;font-weight:800">{dev['total']:,}</td>
                </tr>
                <tr>
                  <td style="padding:10px">Límite mensual</td>
                  <td style="padding:10px;text-align:right">{dev['limite_mes']:,}</td>
                </tr>
                <tr style="background:#f1f5f9">
                  <td style="padding:10px">Promedio por día</td>
                  <td style="padding:10px;text-align:right">{dev['promedio_dia']}</td>
                </tr>
                <tr>
                  <td style="padding:10px">Límite por día</td>
                  <td style="padding:10px;text-align:right">{dev['limite_dia']:,}</td>
                </tr>
              </table>
              <p style="color:#94a3b8;font-size:11px;margin-top:20px">
                Monitor Sigfox — {datos['mes_nombre']} {year}
              </p>
            </body></html>"""
            try:
                _enviar(
                    f"{icono} Dispositivo {nivel_dev}: {dev['id']} al {dev['pct']}% — {datos['mes_nombre']} {year}",
                    html_dev, dests_envio
                )
            except Exception as e:
                print(f"  [Alertas] Error alerta dispositivo {dev['id']}: {e}")

    except Exception as e:
        print(f"  [Alertas] Error general: {e}")
        import traceback; traceback.print_exc()


# ── Iniciar scheduler ─────────────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone="America/Mexico_City")

# Reporte semanal
scheduler.add_job(
    enviar_reporte_semanal,
    trigger="cron",
    day_of_week="fri",
    hour=8,
    minute=0,
    id="reporte_semanal",
    replace_existing=True,
)

# Verificación de alertas: 08:00, 13:00 y 18:00 cada día
scheduler.add_job(
    verificar_y_enviar_alertas,
    trigger="cron",
    hour="8,13,18",
    minute=30,
    id="verificar_alertas",
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
