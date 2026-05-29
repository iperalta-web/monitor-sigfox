# Guía de Despliegue — Monitor Sigfox

## Opción 1 — Railway (RECOMENDADO, gratis para empezar)

Railway.app es la forma más rápida y sencilla. Gratis hasta ~500 horas/mes.

### Pasos

1. **Crear cuenta** en https://railway.app (puedes entrar con GitHub)

2. **Subir el código a GitHub**
   ```bash
   cd /Users/iperalta/Documents/Claude/numero_mensajes_x_meses
   git init
   git add .
   git commit -m "Monitor Sigfox inicial"
   # Crea un repo en github.com y sigue sus instrucciones para subir
   ```

3. **Crear proyecto en Railway**
   - Clic en "New Project" → "Deploy from GitHub repo"
   - Selecciona tu repositorio
   - Railway detecta el `Procfile` automáticamente

4. **Agregar variables de entorno** en Railway → Variables:
   ```
   SECRET_KEY = (genera una clave aleatoria larga, ej: openssl rand -hex 32)
   PORT       = 5000
   ```

5. **Dominio personalizado** (opcional)
   - Railway → Settings → Domains → "Generate Domain"
   - Te da una URL como: `monitor-sigfox.up.railway.app`
   - Puedes conectar tu propio dominio: `monitor.iotnet.mx`

6. **Listo.** La app se despliega en ~2 minutos.

---

## Opción 2 — Render (también gratis)

1. Crear cuenta en https://render.com
2. New → Web Service → conectar GitHub
3. Runtime: Python 3
4. Build command: `pip install -r requirements.txt`
5. Start command: `gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT --timeout 120`
6. Agregar variable de entorno: `SECRET_KEY`

---

## Opción 3 — VPS propio (DigitalOcean / Linode ~$6/mes)

Ideal si quieres control total y múltiples clientes.

### Instalar en servidor Ubuntu

```bash
# 1. Conectarse al servidor
ssh root@TU_IP

# 2. Instalar dependencias
apt update && apt install -y python3-pip python3-venv nginx certbot python3-certbot-nginx

# 3. Copiar archivos (desde tu Mac)
# En tu Mac:
scp -r /Users/iperalta/Documents/Claude/numero_mensajes_x_meses root@TU_IP:/opt/monitor-sigfox

# 4. En el servidor: crear entorno virtual
cd /opt/monitor-sigfox
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 5. Crear archivo de entorno
echo "SECRET_KEY=$(openssl rand -hex 32)" > .env

# 6. Crear servicio systemd para que arranque automático
cat > /etc/systemd/system/monitor-sigfox.service << EOF
[Unit]
Description=Monitor Sigfox
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/monitor-sigfox
EnvironmentFile=/opt/monitor-sigfox/.env
ExecStart=/opt/monitor-sigfox/venv/bin/gunicorn app:app --workers 2 --bind 127.0.0.1:5000 --timeout 120
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable monitor-sigfox
systemctl start monitor-sigfox

# 7. Configurar Nginx como proxy reverso
cat > /etc/nginx/sites-available/monitor-sigfox << EOF
server {
    listen 80;
    server_name monitor.iotnet.mx;  # ← cambia por tu dominio

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    }
}
EOF

ln -s /etc/nginx/sites-available/monitor-sigfox /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# 8. HTTPS gratis con Let's Encrypt
certbot --nginx -d monitor.iotnet.mx
```

---

## Primer acceso

Una vez desplegado, abre la URL y entra con:

| Campo    | Valor     |
|----------|-----------|
| Usuario  | `admin`   |
| Password | `admin123`|

**⚠️ IMPORTANTE: cambia el password inmediatamente en el panel de administración.**

---

## Gestión de usuarios

- Entra como admin → menú **👥 Usuarios** en la barra superior
- Crea un usuario por cliente con rol **Viewer**
- El rol Viewer solo puede **ver** el dashboard, no cambiar configuración
- El rol Admin puede todo, incluyendo crear/eliminar usuarios
- Configura el **límite máximo de usuarios** según tu licencia

---

## Actualizar la app

```bash
# En Railway/Render: basta con hacer push a GitHub
git add . && git commit -m "actualización" && git push

# En VPS:
cd /opt/monitor-sigfox
git pull
systemctl restart monitor-sigfox
```
