# Río Serrano · Motor de Reservas

## Qué hace el proyecto

Backend + frontend para mostrar disponibilidad y permitir reservas del
Hotel Río Serrano (Torres del Paine, Chile), conectándose al motor
oficial de TravelClick / Amadeus iHotelier (código de hotel: 99498).

URL pública: https://rioserrano.onrender.com

## Stack técnico

- Backend: Python + FastAPI
- Frontend: HTML/CSS/JavaScript vanilla (sin frameworks)
- Scraping/automatización: Playwright + Chromium (modo fallback)
- Hosting: Render.com (Docker, plan Free)
- Repo: GitHub - rioserranochile/rioserrano

## Modo dual de operación

El backend tiene DOS modos que cambian automáticamente:

1. Modo OFICIAL (preferido): si están las variables de entorno
   AMADEUS_CLIENT_ID y AMADEUS_CLIENT_SECRET, usa OAuth oficial.
   Rápido, sin Playwright. Reservas funcionan.

2. Modo SCRAPING (fallback actual): captura el JWT del booking
   engine público con Playwright. Solo consultas; reservas dan 503.

Estado actual: modo SCRAPING, esperando credenciales Amadeus.

## Endpoints internos

- GET /                     → sirve index.html
- GET /api/health           → estado del backend
- GET /api/disponibilidad   → busca habitaciones
- POST /api/reservar        → crea reserva (solo modo OFICIAL)

## Archivos clave

- server.py        → backend FastAPI
- index.html       → frontend con 3 pantallas (rooms, bed, guest form)
- Dockerfile       → imagen Docker para Render (incluye Playwright)
- requirements.txt → dependencias Python

## Cómo se despliega

Push a `main` en GitHub → Render redeploya automáticamente.

## Preferencias del usuario

- Yo (Claudio) NO soy desarrollador full-time. Código debe ser SIMPLE.
- Comentarios en español cuando sea necesario.
- Render plan Free preferido.
- Sin frameworks JS pesados (mantener HTML/JS vanilla).
- Idiomas soportados: español (default) e inglés con switch.

## Tareas pendientes

- Esperar credenciales OAuth oficiales de Amadeus
- Cuando lleguen: agregar AMADEUS_CLIENT_ID y AMADEUS_CLIENT_SECRET en Render
- Probar primera reserva real (probable ajuste en crear_reserva())