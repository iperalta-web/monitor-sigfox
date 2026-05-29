#!/usr/bin/env python3
# coding: utf-8
"""
Gestión de base de datos PostgreSQL (Supabase) — usuarios y sesiones
Requiere: psycopg2-binary
Var de entorno: DATABASE_URL  (postgresql://user:pass@host:5432/db)
"""

import os
import hashlib
import secrets
from datetime import datetime

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def init_db():
    conn = get_db()
    cur  = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id          SERIAL PRIMARY KEY,
            username    TEXT UNIQUE NOT NULL,
            password    TEXT NOT NULL,
            nombre      TEXT,
            email       TEXT,
            rol         TEXT DEFAULT 'viewer',
            activo      INTEGER DEFAULT 1,
            creado_en   TEXT,
            ultimo_login TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS config_app (
            clave TEXT PRIMARY KEY,
            valor TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sesiones_activas (
            token       TEXT PRIMARY KEY,
            user_id     INTEGER,
            creada_en   TEXT,
            ultimo_uso  TEXT,
            ip          TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS dispositivos (
            id          SERIAL PRIMARY KEY,
            device_id   TEXT UNIQUE NOT NULL,
            nombre      TEXT,
            activo      INTEGER DEFAULT 1,
            agregado_en TEXT
        )
    """)

    # Config por defecto
    for clave, valor in [('max_usuarios','10'),
                          ('nombre_app','Monitor Sigfox'),
                          ('logo_empresa','IotNet')]:
        cur.execute("""
            INSERT INTO config_app(clave, valor) VALUES(%s, %s)
            ON CONFLICT (clave) DO NOTHING
        """, (clave, valor))

    # Admin por defecto
    cur.execute("SELECT COUNT(*) AS n FROM usuarios")
    if cur.fetchone()["n"] == 0:
        pw = hashlib.sha256("admin123".encode()).hexdigest()
        cur.execute("""
            INSERT INTO usuarios(username, password, nombre, rol, activo, creado_en)
            VALUES (%s, %s, 'Administrador', 'admin', 1, %s)
        """, ('admin', pw, datetime.now().isoformat()))
        print("  [DB] Usuario admin creado. Password: admin123")

    conn.commit()
    cur.close()
    conn.close()


def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


def verificar_usuario(username, password):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM usuarios WHERE username=%s AND activo=1", (username,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row and row["password"] == hash_password(password):
        return dict(row)
    return None


def get_usuario(user_id):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM usuarios WHERE id=%s", (user_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return dict(row) if row else None


def actualizar_ultimo_login(user_id):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("UPDATE usuarios SET ultimo_login=%s WHERE id=%s",
                (datetime.now().isoformat(), user_id))
    conn.commit(); cur.close(); conn.close()


def get_config(clave, default=None):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT valor FROM config_app WHERE clave=%s", (clave,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row["valor"] if row else default


def set_config(clave, valor):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO config_app(clave, valor) VALUES(%s, %s)
        ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor
    """, (clave, str(valor)))
    conn.commit(); cur.close(); conn.close()


# ── CRUD usuarios ─────────────────────────────────────────────────────────────

def listar_usuarios():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT id, username, nombre, email, rol, activo, creado_en, ultimo_login
        FROM usuarios ORDER BY id
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [dict(r) for r in rows]


def contar_usuarios_activos():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM usuarios WHERE activo=1")
    n = cur.fetchone()["n"]
    cur.close(); conn.close()
    return n


def crear_usuario(username, password, nombre, email, rol="viewer"):
    conn = get_db()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO usuarios(username, password, nombre, email, rol, activo, creado_en)
            VALUES (%s,%s,%s,%s,%s,1,%s)
        """, (username, hash_password(password), nombre, email, rol,
              datetime.now().isoformat()))
        conn.commit()
        return True, "Usuario creado"
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return False, "El usuario ya existe"
    finally:
        cur.close(); conn.close()


def actualizar_usuario(user_id, nombre=None, email=None, rol=None, activo=None, password=None):
    conn = get_db()
    cur  = conn.cursor()
    if nombre   is not None: cur.execute("UPDATE usuarios SET nombre=%s   WHERE id=%s", (nombre,   user_id))
    if email    is not None: cur.execute("UPDATE usuarios SET email=%s    WHERE id=%s", (email,    user_id))
    if rol      is not None: cur.execute("UPDATE usuarios SET rol=%s      WHERE id=%s", (rol,      user_id))
    if activo   is not None: cur.execute("UPDATE usuarios SET activo=%s   WHERE id=%s", (activo,   user_id))
    if password is not None: cur.execute("UPDATE usuarios SET password=%s WHERE id=%s",
                                         (hash_password(password), user_id))
    conn.commit(); cur.close(); conn.close()


def eliminar_usuario(user_id):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM usuarios WHERE id=%s AND rol != 'admin'", (user_id,))
    conn.commit(); cur.close(); conn.close()


# ── CRUD dispositivos ─────────────────────────────────────────────────────────

def listar_dispositivos(solo_activos=True):
    conn = get_db()
    cur  = conn.cursor()
    q = "SELECT * FROM dispositivos"
    if solo_activos:
        q += " WHERE activo=1"
    q += " ORDER BY device_id"
    cur.execute(q)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [dict(r) for r in rows]


def get_ids_dispositivos():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT device_id FROM dispositivos WHERE activo=1 ORDER BY device_id")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [r["device_id"] for r in rows]


def contar_dispositivos():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM dispositivos WHERE activo=1")
    n = cur.fetchone()["n"]
    cur.close(); conn.close()
    return n


def importar_dispositivos_csv(texto_csv, reemplazar=False):
    import csv, io
    conn = get_db()
    cur  = conn.cursor()
    if reemplazar:
        cur.execute("DELETE FROM dispositivos")
        conn.commit()

    insertados = duplicados = errores = 0
    reader = csv.DictReader(io.StringIO(texto_csv))
    col = None
    for posible in ["IDs", "ID", "device_id", "DeviceId", "id", "Device ID"]:
        if posible in (reader.fieldnames or []):
            col = posible
            break
    if col is None and reader.fieldnames:
        col = reader.fieldnames[0]

    now = datetime.now().isoformat()
    for row in reader:
        try:
            dev_id = str(row[col]).strip()
            if not dev_id:
                continue
            cur.execute("""
                INSERT INTO dispositivos(device_id, activo, agregado_en)
                VALUES(%s, 1, %s)
                ON CONFLICT (device_id) DO NOTHING
            """, (dev_id, now))
            if cur.rowcount:
                insertados += 1
            else:
                duplicados += 1
        except Exception:
            errores += 1
    conn.commit()
    cur.close(); conn.close()
    return insertados, duplicados, errores


def agregar_dispositivo(device_id, nombre=""):
    conn = get_db()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO dispositivos(device_id, nombre, activo, agregado_en)
            VALUES(%s, %s, 1, %s)
        """, (device_id.strip(), nombre.strip(), datetime.now().isoformat()))
        conn.commit()
        return True, "Dispositivo agregado"
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return False, "El dispositivo ya existe"
    finally:
        cur.close(); conn.close()


def eliminar_dispositivo(device_id):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM dispositivos WHERE device_id=%s", (device_id,))
    conn.commit(); cur.close(); conn.close()


def toggle_dispositivo(device_id, activo):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("UPDATE dispositivos SET activo=%s WHERE device_id=%s", (activo, device_id))
    conn.commit(); cur.close(); conn.close()
