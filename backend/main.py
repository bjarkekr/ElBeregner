import os
import json
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import secrets
import httpx
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.middleware.cors import CORSMiddleware

from fastapi.responses import JSONResponse
from starlette.requests import Request

app = FastAPI(title="ElBeregner API", version="1.0.0")

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": f"Serverfejl: {type(exc).__name__}: {str(exc)}"},
        headers={"Access-Control-Allow-Origin": "*"},
    )

# Config from environment
ELOVERBLIK_TOKEN = os.getenv("ELOVERBLIK_TOKEN", "").strip()
ELAFGIFT_ORE = float(os.getenv("ELAFGIFT_ORE", "76.1"))
NETTARIF_T1_ORE = float(os.getenv("NETTARIF_T1_ORE", "30.0"))    # 00-06 lavlast
NETTARIF_T2_ORE = float(os.getenv("NETTARIF_T2_ORE", "92.0"))    # 06-17 høj
NETTARIF_T3_ORE = float(os.getenv("NETTARIF_T3_ORE", "318.0"))   # 17-21 spidslast
NETTARIF_T4_ORE = float(os.getenv("NETTARIF_T4_ORE", "92.0"))    # 21-24 høj
SYSTEMTARIF_ORE = float(os.getenv("SYSTEMTARIF_ORE", "6.0"))      # Energinet systemtarif
TRANSMISSIONSTARIF_ORE = float(os.getenv("TRANSMISSIONSTARIF_ORE", "4.9"))  # Energinet transmissionstarif
ELSELSKAB_TILLÆG_ORE = float(os.getenv("ELSELSKAB_TILLÆG_ORE", "0.0"))
ABONNEMENT_KR = float(os.getenv("ABONNEMENT_KR", "38.0"))         # Fast månedligt abonnement
MOMS = float(os.getenv("MOMS", "0.25"))
PRISZONE = os.getenv("PRISZONE", "DK1")
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")
API_KEY = os.getenv("API_KEY", "")

DK_TZ = ZoneInfo("Europe/Copenhagen")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

_token_cache: dict = {"token": None, "expires_at": None}


def check_api_key(x_api_key: Optional[str] = Header(default=None)):
    if not API_KEY:
        return  # Ingen nøgle konfigureret — åben adgang
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Ugyldig eller manglende API-nøgle.")

ELOVERBLIK_BASE = "https://api.eloverblik.dk/CustomerApi/api"
ENERGIDATA_URL = "https://api.energidataservice.dk/dataset/Elspotprices"


def nettarif_for_hour(utc_iso: str) -> float:
    """Returnerer nettarif i øre/kWh baseret på dansk lokaltid."""
    dt_utc = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    dt_dk = dt_utc.astimezone(DK_TZ)
    h = dt_dk.hour
    if 0 <= h < 6:
        return NETTARIF_T1_ORE
    elif 6 <= h < 17:
        return NETTARIF_T2_ORE
    elif 17 <= h < 21:
        return NETTARIF_T3_ORE
    else:
        return NETTARIF_T4_ORE


async def get_access_token() -> str:
    now = datetime.utcnow()
    if (
        _token_cache["token"]
        and _token_cache["expires_at"]
        and now < _token_cache["expires_at"]
    ):
        return _token_cache["token"]

    if not ELOVERBLIK_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="ELOVERBLIK_TOKEN er ikke konfigureret på serveren.",
        )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{ELOVERBLIK_BASE}/token",
            headers={"Authorization": f"Bearer {ELOVERBLIK_TOKEN}"},
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Eloverblik returnerede fejl ved token-hentning: {resp.status_code}",
        )

    data = resp.json()
    token = data.get("result")
    if not token:
        raise HTTPException(status_code=502, detail="Eloverblik returnerede intet token.")

    _token_cache["token"] = token
    _token_cache["expires_at"] = now + timedelta(hours=23)
    return token


async def get_metering_point_id(token: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{ELOVERBLIK_BASE}/meteringpoints/meteringpoints?includeAll=false",
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Eloverblik målepunkt fejl: {resp.status_code}",
        )

    points = resp.json().get("result", [])
    if not points:
        raise HTTPException(status_code=404, detail="Ingen målepunkter fundet på kontoen.")
    return points[0]["meteringPointId"]


def parse_timeseries(result: list) -> dict[str, float]:
    consumption: dict[str, float] = {}
    for item in result:
        doc = item.get("MyEnergyData_MarketDocument", {})
        for ts in doc.get("TimeSeries", []):
            for period in ts.get("Period", []):
                interval_start = period.get("timeInterval", {}).get("start", "")
                if not interval_start:
                    continue
                try:
                    start_dt = datetime.fromisoformat(interval_start.replace("Z", "+00:00"))
                except ValueError:
                    continue
                for point in period.get("Point", []):
                    pos = int(point.get("position", 1)) - 1
                    qty_raw = point.get("out_Quantity.quantity", "0") or "0"
                    try:
                        kwh = float(qty_raw)
                    except ValueError:
                        kwh = 0.0
                    hour_dt = start_dt + timedelta(hours=pos)
                    key = hour_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                    consumption[key] = consumption.get(key, 0.0) + kwh
    return consumption


@app.get("/api/forbrug")
async def get_forbrug(
    fra: str = Query(..., description="YYYY-MM-DD"),
    til: str = Query(..., description="YYYY-MM-DD"),
    x_api_key: Optional[str] = Header(default=None),
):
    check_api_key(x_api_key)
    try:
        datetime.strptime(fra, "%Y-%m-%d")
        datetime.strptime(til, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Dato skal være YYYY-MM-DD")

    token = await get_access_token()
    mp_id = await get_metering_point_id(token)
    body = {"meteringPoints": {"meteringPoint": [mp_id]}}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{ELOVERBLIK_BASE}/meterdata/gettimeseries/{fra}/{til}/Hour",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Eloverblik tidsseriedata fejl: {resp.status_code} - {resp.text[:300]}",
        )

    consumption = parse_timeseries(resp.json().get("result", []))
    return {
        "fra": fra,
        "til": til,
        "maalerid": mp_id,
        "timeforbrug": consumption,
        "total_kwh": round(sum(consumption.values()), 3),
    }


@app.get("/api/spotpriser")
async def get_spotpriser(
    fra: str = Query(..., description="YYYY-MM-DD"),
    til: str = Query(..., description="YYYY-MM-DD"),
    zone: Optional[str] = Query(None, description="DK1 eller DK2"),
    x_api_key: Optional[str] = Header(default=None),
):
    check_api_key(x_api_key)
    try:
        datetime.strptime(fra, "%Y-%m-%d")
        datetime.strptime(til, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Dato skal være YYYY-MM-DD")

    pris_zone = zone or PRISZONE
    til_excl = (datetime.strptime(til, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    params = {
        "start": f"{fra}T00:00",
        "end": f"{til_excl}T00:00",
        "filter": json.dumps({"PriceArea": pris_zone}),
        "sort": "HourUTC asc",
        "limit": 2000,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(ENERGIDATA_URL, params=params)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Energi Data Service fejl: {resp.status_code}")

    priser: dict[str, float] = {}
    for r in resp.json().get("records", []):
        hour_utc = r.get("HourUTC", "")
        if hour_utc:
            key = hour_utc.replace(" ", "T")
            if not key.endswith("Z"):
                key += "Z"
            priser[key] = round((r.get("SpotPriceDKK") or 0.0) / 1000, 6)

    return {"fra": fra, "til": til, "zone": pris_zone, "spotpriser": priser}


@app.get("/api/maaned")
async def get_maaned(
    aar: int = Query(..., description="Årstal, f.eks. 2024"),
    maaned: int = Query(..., description="Måned 1-12"),
    x_api_key: Optional[str] = Header(default=None),
):
    """Beregn samlet månedspris inkl. alle afgifter og abonnement."""
    check_api_key(x_api_key)
    if not (1 <= maaned <= 12):
        raise HTTPException(status_code=400, detail="Måned skal være 1-12")

    fra_dt = datetime(aar, maaned, 1)
    til_dt = (datetime(aar, maaned + 1, 1) if maaned < 12 else datetime(aar + 1, 1, 1)) - timedelta(days=1)
    # Begræns til i dag — eloverblik har ikke fremtidige data
    today = datetime.utcnow().date()
    if til_dt.date() > today:
        til_dt = datetime.combine(today, datetime.min.time())
    fra = fra_dt.strftime("%Y-%m-%d")
    til = til_dt.strftime("%Y-%m-%d")

    forbrug_data, spot_data = await asyncio.gather(
        asyncio.create_task(_fetch_forbrug_raw(fra, til)),
        asyncio.create_task(_fetch_spotpriser_raw(fra, til)),
    )

    timeforbrug = forbrug_data["timeforbrug"]
    spotpriser = spot_data["spotpriser"]

    # Fast tillæg pr. kWh (uden nettarif, som er tidsafhængig)
    fast_ore = ELAFGIFT_ORE + SYSTEMTARIF_ORE + TRANSMISSIONSTARIF_ORE + ELSELSKAB_TILLÆG_ORE

    timer: list[dict] = []
    total_kr_forbrug = 0.0
    total_kwh = 0.0
    manglende_timer: list[str] = []
    spotpris_sum = 0.0
    spotpris_count = 0

    # Iterer kun over timer med forbrugsdata — spotpriser kan have ekstra timer
    for hour in sorted(timeforbrug.keys()):
        kwh = timeforbrug[hour]
        spot = spotpriser.get(hour)

        if spot is None:
            manglende_timer.append(hour)
            continue

        nettarif = nettarif_for_hour(hour)
        # Spotpris er ekskl. moms; brugerens tariffer er inkl. moms
        pris_per_kwh = spot * (1 + MOMS) + (fast_ore + nettarif) / 100.0
        kr = round(kwh * pris_per_kwh, 4)

        total_kr_forbrug += kr
        total_kwh += kwh
        spotpris_sum += spot
        spotpris_count += 1

        timer.append({
            "time": hour,
            "kwh": round(kwh, 4),
            "spotpris_kwh": round(spot, 6),
            "nettarif_ore": nettarif,
            "pris_per_kwh": round(pris_per_kwh, 4),
            "kr": kr,
        })

    gns_spotpris = (spotpris_sum / spotpris_count) if spotpris_count else 0.0

    # Abonnement er allerede inkl. moms
    total_kr = round(total_kr_forbrug + ABONNEMENT_KR, 2)

    return {
        "aar": aar,
        "maaned": maaned,
        "fra": fra,
        "til": til,
        "zone": PRISZONE,
        "total_kwh": round(total_kwh, 3),
        "total_kr_forbrug": round(total_kr_forbrug, 2),
        "abonnement_kr": ABONNEMENT_KR,
        "total_kr": total_kr,
        "gns_spotpris_kwh": round(gns_spotpris, 6),
        "afgifter": {
            "elafgift_ore": ELAFGIFT_ORE,
            "nettarif_t1_ore": NETTARIF_T1_ORE,
            "nettarif_t2_ore": NETTARIF_T2_ORE,
            "nettarif_t3_ore": NETTARIF_T3_ORE,
            "nettarif_t4_ore": NETTARIF_T4_ORE,
            "systemtarif_ore": SYSTEMTARIF_ORE,
            "transmissionstarif_ore": TRANSMISSIONSTARIF_ORE,
            "elselskab_tillæg_ore": ELSELSKAB_TILLÆG_ORE,
            "abonnement_kr": ABONNEMENT_KR,
            "moms_pct": MOMS * 100,
        },
        "timer": timer,
        "manglende_timer_antal": len(manglende_timer),
    }


@app.get("/api/debug/eloverblik")
async def debug_eloverblik():
    """Rådata fra eloverblik til fejlfinding."""
    token = await get_access_token()
    mp_id = await get_metering_point_id(token)
    body = {"meteringPoints": {"meteringPoint": [mp_id]}}
    fra = "2026-05-01"
    til = "2026-05-03"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{ELOVERBLIK_BASE}/meterdata/gettimeseries/{fra}/{til}/Hour",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
        )
    return {"status": resp.status_code, "maalerid": mp_id, "raa_svar": resp.json()}


@app.get("/api/status")
async def status():
    return {
        "status": "ok",
        "zone": PRISZONE,
        "afgifter": {
            "elafgift_ore": ELAFGIFT_ORE,
            "nettarif_t1_00_06": NETTARIF_T1_ORE,
            "nettarif_t2_06_17": NETTARIF_T2_ORE,
            "nettarif_t3_17_21": NETTARIF_T3_ORE,
            "nettarif_t4_21_24": NETTARIF_T4_ORE,
            "systemtarif_ore": SYSTEMTARIF_ORE,
            "transmissionstarif_ore": TRANSMISSIONSTARIF_ORE,
            "elselskab_tillæg_ore": ELSELSKAB_TILLÆG_ORE,
            "abonnement_kr": ABONNEMENT_KR,
        },
    }


async def _fetch_forbrug_raw(fra: str, til: str) -> dict:
    token = await get_access_token()
    mp_id = await get_metering_point_id(token)
    body = {"meteringPoints": {"meteringPoint": [mp_id]}}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{ELOVERBLIK_BASE}/meterdata/gettimeseries/{fra}/{til}/Hour",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Eloverblik tidsseriedata fejl: {resp.status_code}")

    return {"timeforbrug": parse_timeseries(resp.json().get("result", []))}


async def _fetch_spotpriser_raw(fra: str, til: str) -> dict:
    til_excl = (datetime.strptime(til, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    params = {
        "start": f"{fra}T00:00",
        "end": f"{til_excl}T00:00",
        "filter": json.dumps({"PriceArea": PRISZONE}),
        "sort": "HourUTC asc",
        "limit": 2000,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(ENERGIDATA_URL, params=params)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Energi Data Service fejl: {resp.status_code}")

    priser: dict[str, float] = {}
    for r in resp.json().get("records", []):
        hour_utc = r.get("HourUTC", "")
        if hour_utc:
            key = hour_utc.replace(" ", "T")
            if not key.endswith("Z"):
                key += "Z"
            priser[key] = round((r.get("SpotPriceDKK") or 0.0) / 1000, 6)

    return {"spotpriser": priser}
