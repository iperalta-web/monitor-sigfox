#!/usr/bin/env python3
# coding: utf-8
"""
Gestión de base de datos SQLite — usuarios y sesiones
"""

import sqlite3
import hashlib
import os
import secrets
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "data", "usuarios.db")


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT UNIQUE NOT NULL,
            password    TEXT NOT NULL,
            nombre      TEXT,
            email       TEXT,
            rol         TEXT DEFAULT 'viewer',
            activo      INTEGER DEFAULT 1,
            creado_en   TEXT,
            ultimo_login TEXT
        );

        CREATE TABLE IF NOT EXISTS config_app (
            clave TEXT PRIMARY KEY,
            valor TEXT
        );

        CREATE TABLE IF NOT EXISTS sesiones_activas (
            token       TEXT PRIMARY KEY,
            user_id     INTEGER,
            creada_en   TEXT,
            ultimo_uso  TEXT,
            ip          TEXT
        );
    """)

    # Config por defecto
    conn.execute("""
        INSERT OR IGNORE INTO config_app(clave, valor) VALUES
        ('max_usuarios', '10'),
        ('nombre_app', 'Monitor Sigfox'),
        ('logo_empresa', 'IotNet')
    """)

    # Admin por defecto (solo si no existe ningún usuario)
    existe = conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
    if existe == 0:
        pw = hashlib.sha256("admin123".encode()).hexdigest()
        conn.execute("""
            INSERT INTO usuarios(username, password, nombre, rol, activo, creado_en)
            VALUES ('admin', ?, 'Administrador', 'admin', 1, ?)
        """, (pw, datetime.now().isoformat()))
        print("  [DB] Usuario admin creado. Password: admin123")

    conn.commit()
    conn.close()


def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


def verificar_usuario(username, password):
    conn = get_db()
    row = conn.execute("""
        SELECT * FROM usuarios WHERE username=? AND activo=1
    """, (username,)).fetchone()
    conn.close()
    if row and row["password"] == hash_password(password):
        return dict(row)
    return None


def get_usuario(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM usuarios WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def actualizar_ultimo_login(user_id):
    conn = get_db()
    conn.execute("UPDATE usuarios SET ultimo_login=? WHERE id=?",
                 (datetime.now().isoformat(), user_id))
    conn.commit()
    conn.close()


def get_config(clave, default=None):
    conn = get_db()
    row = conn.execute("SELECT valor FROM config_app WHERE clave=?", (clave,)).fetchone()
    conn.close()
    return row["valor"] if row else default


def set_config(clave, valor):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO config_app(clave,valor) VALUES(?,?)", (clave, str(valor)))
    conn.commit()
    conn.close()


# ── CRUD usuarios ─────────────────────────────────────────────────────────────

def listar_usuarios():
    conn = get_db()
    rows = conn.execute("""
        SELECT id, username, nombre, email, rol, activo, creado_en, ultimo_login
        FROM usuarios ORDER BY id
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def contar_usuarios_activos():
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM usuarios WHERE activo=1").fetchone()[0]
    conn.close()
    return n


def crear_usuario(username, password, nombre, email, rol="viewer"):
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO usuarios(username, password, nombre, email, rol, activo, creado_en)
            VALUES (?,?,?,?,?,1,?)
        """, (username, hash_password(password), nombre, email, rol, datetime.now().isoformat()))
        conn.commit()
        return True, "Usuario creado"
    except sqlite3.IntegrityError:
        return False, "El usuario ya existe"
    finally:
        conn.close()


def actualizar_usuario(user_id, nombre=None, email=None, rol=None, activo=None, password=None):
    conn = get_db()
    if nombre   is not None: conn.execute("UPDATE usuarios SET nombre=?  WHERE id=?", (nombre,   user_id))
    if email    is not None: conn.execute("UPDATE usuarios SET email=?   WHERE id=?", (email,    user_id))
    if rol      is not None: conn.execute("UPDATE usuarios SET rol=?     WHERE id=?", (rol,      user_id))
    if activo   is not None: conn.execute("UPDATE usuarios SET activo=?  WHERE id=?", (activo,   user_id))
    if password is not None: conn.execute("UPDATE usuarios SET password=? WHERE id=?", (hash_password(password), user_id))
    conn.commit()
    conn.close()


def eliminar_usuario(user_id):
    conn = get_db()
    conn.execute("DELETE FROM usuarios WHERE id=? AND rol != 'admin'", (user_id,))
    conn.commit()
    conn.close()
