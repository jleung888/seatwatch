import datetime as dt
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Query

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

from seats_watch import AMADEUS_DEFAULT_HOST, AmadeusClient

app = FastAPI()


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


def _get_client() -> AmadeusClient:
    client_id = os.getenv("AMADEUS_CLIENT_ID")
    client_secret = os.getenv("AMADEUS_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Missing AMADEUS_CLIENT_ID/AMADEUS_CLIENT_SECRET")
    return AmadeusClient(client_id=client_id, client_secret=client_secret, host=AMADEUS_DEFAULT_HOST)


def _iter_dates(start: dt.date, end: dt.date):
    cur = start
    one = dt.timedelta(days=1)
    while cur <= end:
        yield cur
        cur += one


def _offer_seats(offer: dict[str, Any]) -> int | None:
    seats = offer.get("numberOfBookableSeats")
    if seats is None:
        return None
    try:
        return int(seats)
    except Exception:
        return None


@app.get("/check")
def check(
    origin: str = Query(..., min_length=3, max_length=3),
    dest: str = Query(..., min_length=3, max_length=3),
    start: dt.date = Query(...),
    end: dt.date = Query(...),
) -> dict[str, Any]:
    if end < start:
        raise HTTPException(status_code=400, detail="end must be on/after start")

    client = _get_client()
    results: list[dict[str, Any]] = []

    for day in _iter_dates(start, end):
        try:
            payload = client.flight_offers_search(origin=origin.upper(), destination=dest.upper(), departure_date=day, adults=1, max_results=250)
            offers = payload.get("data") or []
            if not isinstance(offers, list):
                offers = []

            low = []
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                seats = _offer_seats(offer)
                if seats is None:
                    continue
                if seats == 9:
                    continue  # 9+ / unknown; do not treat as <= 9
                if seats < 9:
                    low.append(seats)

            if low:
                results.append(
                    {
                        "date": day.isoformat(),
                        "min_bookable": min(low),
                        "count_low_offers": len(low),
                    }
                )
        except Exception as exc:
            results.append({"date": day.isoformat(), "error": str(exc)})

    return {"origin": origin.upper(), "dest": dest.upper(), "start": start.isoformat(), "end": end.isoformat(), "results": results}

