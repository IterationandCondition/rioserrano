# Río Serrano · Disponibilidad v2

Actualización: ahora muestra el detalle por tipo de habitación con imágenes, amenidades y tarifas por rate plan (Full Board, All Inclusive).

## Cambios vs v1

- **Endpoint nuevo**: `GET /ibe-shop/v1/hotel/99498/avail` (en lugar de `basicavail/multi-room`)
- **Frontend rediseñado**: tarjetas de habitación con imagen, descripción, features, amenidades y tarifas
- **Sin calendario adicional**: foco en mostrar habitaciones para las fechas seleccionadas
- **Default**: arranca desde septiembre 2026 (temporada Río Serrano)

## Cómo actualizar Render

Tienes dos opciones:

### Opción A: Reemplazar archivos en el mismo repo de GitHub

1. En GitHub abre tu repo `rioserrano-disponibilidad`.
2. Para cada archivo (server.py, index.html), click en el archivo → ícono lápiz (Edit) → borra todo → pega el contenido nuevo → Commit changes.
3. Render redeploya automáticamente. Toma 5-10 min.

### Opción B: Borrar y subir de nuevo

1. En GitHub abre el repo → cada archivo → ícono basura → Commit.
2. Re-sube los archivos del ZIP arrastrándolos como hicimos antes.

Render detecta el push y redeploya solo.
