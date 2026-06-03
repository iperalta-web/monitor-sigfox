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

def _get_url():
    url = os.environ.get("DATABASE_URL", "")
    # psycopg2 requiere postgresql://, no postgres://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def get_db():
    conn = psycopg2.connect(_get_url(), cursor_factory=psycopg2.extras.RealDictCursor)
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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS proyectos (
            id                      SERIAL PRIMARY KEY,
            nombre                  TEXT NOT NULL,
            descripcion             TEXT,
            cliente                 TEXT,
            limite_global           INTEGER DEFAULT 5000000,
            activo                  INTEGER DEFAULT 1,
            creado_en               TEXT,
            dispositivos_contratados INTEGER DEFAULT 0,
            limite_diario_dispositivo INTEGER DEFAULT 0
        )
    """)
    # Migración: agregar columnas si no existen (idempotente)
    for col, tipo, default in [
        ("dispositivos_contratados",    "INTEGER", "0"),
        ("limite_diario_dispositivo",   "INTEGER", "0"),
    ]:
        try:
            cur.execute(f"ALTER TABLE proyectos ADD COLUMN {col} {tipo} DEFAULT {default}")
        except Exception:
            pass  # ya existe

    for col, defn in [("modo_calculo", "VARCHAR(20) DEFAULT 'pool'")]:
        try:
            cur.execute(f"ALTER TABLE proyectos ADD COLUMN {col} {defn}")
            conn.commit()
        except Exception:
            conn.rollback()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS proyecto_dispositivos (
            proyecto_id  INTEGER REFERENCES proyectos(id) ON DELETE CASCADE,
            device_id    TEXT    REFERENCES dispositivos(device_id) ON DELETE CASCADE,
            PRIMARY KEY (proyecto_id, device_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuario_proyectos (
            user_id     INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
            proyecto_id INTEGER REFERENCES proyectos(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, proyecto_id)
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

    # Proyecto default: si no existe ninguno, crear uno y migrar dispositivos existentes
    cur.execute("SELECT COUNT(*) AS n FROM proyectos")
    if cur.fetchone()["n"] == 0:
        cur.execute("""
            INSERT INTO proyectos(nombre, descripcion, cliente, limite_global, activo, creado_en)
            VALUES ('Default', 'Proyecto por defecto', 'General', 5000000, 1, %s)
            RETURNING id
        """, (datetime.now().isoformat(),))
        pid = cur.fetchone()["id"]
        # Asignar todos los dispositivos existentes al proyecto default
        cur.execute("SELECT device_id FROM dispositivos")
        for row in cur.fetchall():
            cur.execute("""
                INSERT INTO proyecto_dispositivos(proyecto_id, device_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
            """, (pid, row["device_id"]))
        print(f"  [DB] Proyecto 'Default' creado (id={pid}) con dispositivos existentes.")

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


# ── CRUD proyectos ────────────────────────────────────────────────────────────

def listar_proyectos(solo_activos=True):
    conn = get_db(); cur = conn.cursor()
    q = "SELECT * FROM proyectos"
    if solo_activos:
        q += " WHERE activo=1"
    q += " ORDER BY nombre"
    cur.execute(q)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [dict(r) for r in rows]


def get_proyecto(proyecto_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM proyectos WHERE id=%s", (proyecto_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return dict(row) if row else None


def crear_proyecto(nombre, descripcion="", cliente="", limite_global=5000000,
                   dispositivos_contratados=0, limite_diario_dispositivo=0,
                   modo_calculo='pool'):
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO proyectos(nombre, descripcion, cliente, limite_global, activo, creado_en,
                                  dispositivos_contratados, limite_diario_dispositivo, modo_calculo)
            VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s) RETURNING id
        """, (nombre.strip(), descripcion.strip(), cliente.strip(),
              int(limite_global), datetime.now().isoformat(),
              int(dispositivos_contratados), int(limite_diario_dispositivo), modo_calculo))
        pid = cur.fetchone()["id"]
        conn.commit()
        return True, pid
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        cur.close(); conn.close()


def actualizar_proyecto(proyecto_id, nombre=None, descripcion=None,
                        cliente=None, limite_global=None, activo=None,
                        dispositivos_contratados=None, limite_diario_dispositivo=None,
                        modo_calculo=None):
    conn = get_db(); cur = conn.cursor()
    if nombre        is not None: cur.execute("UPDATE proyectos SET nombre=%s        WHERE id=%s", (nombre,        proyecto_id))
    if descripcion   is not None: cur.execute("UPDATE proyectos SET descripcion=%s   WHERE id=%s", (descripcion,   proyecto_id))
    if cliente       is not None: cur.execute("UPDATE proyectos SET cliente=%s       WHERE id=%s", (cliente,       proyecto_id))
    if limite_global is not None: cur.execute("UPDATE proyectos SET limite_global=%s WHERE id=%s", (int(limite_global), proyecto_id))
    if activo        is not None: cur.execute("UPDATE proyectos SET activo=%s        WHERE id=%s", (activo,        proyecto_id))
    if dispositivos_contratados  is not None:
        cur.execute("UPDATE proyectos SET dispositivos_contratados=%s  WHERE id=%s", (int(dispositivos_contratados),  proyecto_id))
    if limite_diario_dispositivo is not None:
        cur.execute("UPDATE proyectos SET limite_diario_dispositivo=%s WHERE id=%s", (int(limite_diario_dispositivo), proyecto_id))
    if modo_calculo is not None:
        cur.execute("UPDATE proyectos SET modo_calculo=%s WHERE id=%s", (modo_calculo, proyecto_id))
    conn.commit(); cur.close(); conn.close()


def eliminar_proyecto(proyecto_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM proyectos WHERE id=%s", (proyecto_id,))
    conn.commit(); cur.close(); conn.close()


def contar_proyectos():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM proyectos WHERE activo=1")
    n = cur.fetchone()["n"]; cur.close(); conn.close()
    return n


# ── Dispositivos por proyecto ─────────────────────────────────────────────────

def get_dispositivos_proyecto(proyecto_id):
    """Retorna lista de device_ids activos asignados al proyecto."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT d.device_id FROM dispositivos d
        JOIN proyecto_dispositivos pd ON pd.device_id = d.device_id
        WHERE pd.proyecto_id = %s AND d.activo = 1
        ORDER BY d.device_id
    """, (proyecto_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [r["device_id"] for r in rows]


def get_dispositivos_proyecto_detalle(proyecto_id):
    """Retorna dispositivos con detalle (incluyendo inactivos) del proyecto."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT d.* FROM dispositivos d
        JOIN proyecto_dispositivos pd ON pd.device_id = d.device_id
        WHERE pd.proyecto_id = %s ORDER BY d.device_id
    """, (proyecto_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [dict(r) for r in rows]


def asignar_dispositivos_proyecto(proyecto_id, device_ids):
    """Reemplaza la lista completa de dispositivos del proyecto."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM proyecto_dispositivos WHERE proyecto_id=%s", (proyecto_id,))
    for did in device_ids:
        cur.execute("""
            INSERT INTO proyecto_dispositivos(proyecto_id, device_id)
            VALUES (%s, %s) ON CONFLICT DO NOTHING
        """, (proyecto_id, did))
    conn.commit(); cur.close(); conn.close()


def agregar_dispositivo_proyecto(proyecto_id, device_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO proyecto_dispositivos(proyecto_id, device_id)
        VALUES (%s, %s) ON CONFLICT DO NOTHING
    """, (proyecto_id, device_id))
    conn.commit(); cur.close(); conn.close()


def quitar_dispositivo_proyecto(proyecto_id, device_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM proyecto_dispositivos WHERE proyecto_id=%s AND device_id=%s",
                (proyecto_id, device_id))
    conn.commit(); cur.close(); conn.close()


def get_proyectos_de_dispositivo(device_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.nombre FROM proyectos p
        JOIN proyecto_dispositivos pd ON pd.proyecto_id = p.id
        WHERE pd.device_id = %s
    """, (device_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [dict(r) for r in rows]


# ── Usuarios por proyecto ─────────────────────────────────────────────────────

def get_proyectos_de_usuario(user_id):
    """Proyectos asignados a un usuario viewer."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT p.* FROM proyectos p
        JOIN usuario_proyectos up ON up.proyecto_id = p.id
        WHERE up.user_id = %s AND p.activo = 1
        ORDER BY p.nombre
    """, (user_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [dict(r) for r in rows]


def get_proyectos_visibles(user_id, rol):
    """Admin ve todos; viewer solo los asignados."""
    if rol == "admin":
        return listar_proyectos(solo_activos=True)
    return get_proyectos_de_usuario(user_id)


def asignar_usuarios_proyecto(proyecto_id, user_ids):
    """Reemplaza la lista completa de usuarios del proyecto."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM usuario_proyectos WHERE proyecto_id=%s", (proyecto_id,))
    for uid in user_ids:
        cur.execute("""
            INSERT INTO usuario_proyectos(user_id, proyecto_id)
            VALUES (%s, %s) ON CONFLICT DO NOTHING
        """, (uid, proyecto_id))
    conn.commit(); cur.close(); conn.close()


def get_usuarios_de_proyecto(proyecto_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.username, u.nombre, u.rol FROM usuarios u
        JOIN usuario_proyectos up ON up.user_id = u.id
        WHERE up.proyecto_id = %s
    """, (proyecto_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [dict(r) for r in rows]
