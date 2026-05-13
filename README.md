# Río Serrano · Consulta de Disponibilidad

Landing + API en un solo servicio. Una sola plataforma: **Render.com**.

## Deploy en 5 minutos

### 1. Subir a GitHub

Crea un repo nuevo y sube todos estos archivos al root del repo:
- `server.py`
- `index.html`
- `Dockerfile`
- `requirements.txt`

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/TU-USUARIO/TU-REPO.git
git branch -M main
git push -u origin main
```

### 2. Deploy en Render

1. Entra a https://render.com y conecta tu GitHub.
2. **New +** → **Web Service** → selecciona tu repo.
3. Configuración:
   - **Name**: `rioserrano` (o el que prefieras)
   - **Region**: Oregon
   - **Branch**: `main`
   - **Root Directory**: (déjalo vacío)
   - **Runtime**: `Docker` (Render lo detecta solo)
   - **Plan**: `Free`
4. **Create Web Service**.

Render tarda 5-10 minutos en buildear (Playwright + Chromium pesan). Cuando termine tendrás:

```
https://rioserrano.onrender.com
```

Esa URL es tu sitio público completo. Abrirla muestra la landing; el formulario llama a `/api/disponibilidad` en el mismo dominio.

---

## Limitación del plan Free

El servicio **se duerme tras 15 minutos sin tráfico**. La primera consulta después de dormirse tarda 30-60 segundos en responder (Render arranca el contenedor + Playwright captura el JWT). Las consultas siguientes son rápidas.

Para evitarlo:
- **Plan Starter de Render**: $7/mes, siempre activo.
- **Truco gratis**: configura https://cron-job.org para hacer GET a `https://TU-URL.onrender.com/api/health` cada 10 minutos. Mantiene el servicio despierto sin costo.

---

## Archivos

```
.
├── server.py         ← FastAPI + Playwright. Sirve / e /api/disponibilidad
├── index.html        ← Landing
├── Dockerfile        ← Imagen con Chromium preinstalado
├── requirements.txt  ← Dependencias Python
└── README.md
```
