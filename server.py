"""
Servidor único Río Serrano — sirve tanto el frontend (index.html) como la API.
Deploy: Render.com con Docker. Una sola URL pública.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from playwright.async_api import async_playwright

# =========================================================
# Configuración
# =========================================================
HOTEL_ID = 99498
BOOKING_URL = f"https://bookings.travelclick.com/{HOTEL_ID}"
API_AVAIL = f"https://api.travelclick.com/ibe-shop/v1/hotel/{HOTEL_ID}/basicavail/multi-room"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

TOKEN_TTL_SECONDS = 50 * 60
CACHE_TTL_SECONDS = 10 * 60

HERE = Path(__file__).parent


# =========================================================
# JWT cache
# =========================================================
class _TokenCache:
    def __init__(self):
        self.jwt: Optional[str] = None
        self.captured_at: float = 0.0
        self.lock = threading.Lock()

    def expired(self) -> bool:
        return (not self.jwt) or (time.time() - self.captured_at > TOKEN_TTL_SECONDS)


_token = _TokenCache()


async def _capturar_jwt_async() -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
            ],
        )
        ctx = await browser.new_context(
            user_agent=UA,
            viewport={'width': 1280, 'height': 900},
            locale='es-CL',
        )
        await ctx.add_init_script(
            'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
        )
        page = await ctx.new_page()

        jwt_found: Optional[str] = None

        async def on_request(req):
            nonlocal jwt_found
            if jwt_found:
                return
            if 'api.travelclick.com' in req.url and req.resource_type in ('fetch', 'xhr'):
                auth = req.headers.get('authorization', '')
                if auth.startswith('Bearer eyJ'):
                    jwt_found = auth[7:]

        page.on('request', on_request)
        await page.goto(BOOKING_URL, wait_until='domcontentloaded', timeout=45000)

        for _ in range(60):
            if jwt_found:
                break
            await asyncio.sleep(0.5)

        await browser.close()
        if not jwt_found:
            raise RuntimeError("No se pudo capturar JWT del motor")
        return jwt_found


def obtener_jwt(force: bool = False) -> str:
    with _token.lock:
        if force or _token.expired():
            _token.jwt = asyncio.run(_capturar_jwt_async())
            _token.captured_at = time.time()
        return _token.jwt


# =========================================================
# Cache de consultas
# =========================================================
_response_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: tuple) -> Optional[dict]:
    with _cache_lock:
        entry = _response_cache.get(key)
        if entry and (time.time() - entry[0]) < CACHE_TTL_SECONDS:
            return entry[1]
    return None


def _cache_put(key: tuple, value: dict) -> None:
    with _cache_lock:
        _response_cache[key] = (time.time(), value)


# =========================================================
# Consulta al motor
# =========================================================
def consultar_motor(date_in: str, date_out: str,
                    adultos: int, ninos: int,
                    currency: str = "USD", lang: str = "EN_US") -> dict:
    jwt = obtener_jwt()
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-CL",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://bookings.travelclick.com",
        "Referer": "https://bookings.travelclick.com/",
        "User-Agent": UA,
        "x-tc-header": f"currency={currency}",
        "sec-ch-ua": '"Chromium";v="141", "Not?A_Brand";v="8"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }
    payload = {
        "hotelCode": HOTEL_ID,
        "lang": lang,
        "currency": currency,
        "bookerIdentifier": "",
        "partnerIdentifier": "Web4_Desktop",
        "dateIn": date_in,
        "dateOut": date_out,
        "multiRoomOccupancy": [{"adults": adultos, "infant": 0, "children": ninos}],
    }
    r = requests.post(API_AVAIL, headers=headers, json=payload, timeout=30)
    if r.status_code == 401:
        jwt = obtener_jwt(force=True)
        headers["Authorization"] = f"Bearer {jwt}"
        r = requests.post(API_AVAIL, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def normalizar(data: dict, check_in: str, check_out: str) -> dict:
    moneda = data.get("currencyCode", "USD")
    dias = []
    for d in data.get("dates", []):
        st = (d.get("availability") or [{}])[0].get("availStatus", "Unknown")
        dias.append({
            "fecha": d["date"],
            "disponible": bool(d.get("isAvailable")),
            "tarifa_minima": d.get("rate", {}).get("minRate") or 0,
            "estado": st,
        })

    estadia = [d for d in dias if check_in <= d["fecha"] < check_out]
    todas_disp = bool(estadia) and all(d["disponible"] for d in estadia)
    total = sum(d["tarifa_minima"] for d in estadia) if todas_disp else 0
    noches = len(estadia)

    return {
        "check_in": check_in,
        "check_out": check_out,
        "noches": noches,
        "moneda": moneda,
        "estadia_disponible": todas_disp,
        "total_estadia": round(total, 2),
        "tarifa_promedio_noche": round(total / noches, 2) if noches and total else 0,
        "dias": dias,
    }


# =========================================================
# FastAPI app
# =========================================================
app = FastAPI(title="Río Serrano · Disponibilidad")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "hotel_id": HOTEL_ID,
        "jwt_captured": _token.jwt is not None,
        "jwt_age_seconds": int(time.time() - _token.captured_at) if _token.jwt else None,
    }


@app.get("/api/disponibilidad")
def disponibilidad(
    check_in: str = Query(..., description="YYYY-MM-DD"),
    check_out: str = Query(..., description="YYYY-MM-DD"),
    adultos: int = Query(2, ge=1, le=6),
    ninos: int = Query(0, ge=0, le=4),
):
    try:
        din = datetime.strptime(check_in, "%Y-%m-%d").date()
        dout = datetime.strptime(check_out, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Fechas deben ser YYYY-MM-DD")
    if dout <= din:
        raise HTTPException(400, "check_out debe ser posterior a check_in")
    if din < date.today():
        raise HTTPException(400, "check_in no puede estar en el pasado")
    if (dout - din).days > 30:
        raise HTTPException(400, "Rango máximo 30 noches")

    ventana_in = din.isoformat()
    ventana_out = (dout + timedelta(days=14)).isoformat()

    cache_key = (ventana_in, ventana_out, adultos, ninos)
    cached = _cache_get(cache_key)
    if cached:
        data = cached
    else:
        try:
            data = consultar_motor(ventana_in, ventana_out, adultos, ninos)
            _cache_put(cache_key, data)
        except requests.HTTPError as e:
            raise HTTPException(502, f"Motor de reservas error: {e}")
        except Exception as e:
            raise HTTPException(500, f"Error: {e}")

    return JSONResponse(normalizar(data, check_in, check_out))


# El frontend se sirve desde la raíz del mismo servicio
@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
