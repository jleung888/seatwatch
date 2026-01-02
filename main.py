import datetime as dt
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

from seats_watch import AMADEUS_DEFAULT_HOST, AmadeusClient

app = FastAPI()


@app.get("/", include_in_schema=False)
def root() -> HTMLResponse:
    today = dt.date.today()
    start = today.isoformat()
    end = (today + dt.timedelta(days=1)).isoformat()
    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Seatwatch</title>
    <style>
      body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; }}
      .row {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: end; }}
      label {{ display: block; font-size: 12px; color: #444; margin-bottom: 4px; }}
      input {{ padding: 8px; border: 1px solid #ccc; border-radius: 6px; }}
      button {{ padding: 9px 12px; border: 0; border-radius: 6px; background: #111; color: white; cursor: pointer; }}
      pre {{ background: #f6f8fa; padding: 12px; border-radius: 8px; overflow: auto; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
      th {{ background: #f6f8fa; }}
      .muted {{ color: #666; font-size: 12px; }}
    </style>
  </head>
  <body>
    <h1>Seatwatch</h1>
    <p class="muted">Runs <code>/check</code> and returns dates where <code>numberOfBookableSeats &lt; 9</code> (ignores <code>9</code> as “9+ / unknown”).</p>

    <div class="row">
      <div>
        <label>Origin</label>
        <input id="origin" value="LAX" maxlength="3" size="6" />
      </div>
      <div>
        <label>Dest</label>
        <input id="dest" value="HKG" maxlength="3" size="6" />
      </div>
      <div>
        <label>Start</label>
        <input id="start" type="date" value="{start}" />
      </div>
      <div>
        <label>End</label>
        <input id="end" type="date" value="{end}" />
      </div>
      <div>
        <button id="run">Run</button>
      </div>
    </div>

    <p class="muted">Also available: <a href="/docs">/docs</a>, <a href="/health">/health</a></p>

    <h3>Results</h3>
    <table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Min bookable</th>
          <th># offers (&lt; 9)</th>
          <th>Error</th>
        </tr>
      </thead>
      <tbody id="rows">
        <tr><td colspan="4" class="muted">Click Run…</td></tr>
      </tbody>
    </table>

    <h3>Raw JSON</h3>
    <pre id="out">(not run yet)</pre>

    <script>
      const out = document.getElementById('out');
      const rows = document.getElementById('rows');

      function setRows(items) {{
        rows.innerHTML = '';
        if (!items || items.length === 0) {{
          rows.innerHTML = '<tr><td colspan="4" class="muted">No low-seat dates found.</td></tr>';
          return;
        }}
        for (const item of items) {{
          const date = item.date || '';
          const min = (item.min_bookable === undefined || item.min_bookable === null) ? '' : String(item.min_bookable);
          const count = (item.count_low_offers === undefined || item.count_low_offers === null) ? '' : String(item.count_low_offers);
          const err = item.error ? String(item.error) : '';
          const tr = document.createElement('tr');
          tr.innerHTML = `<td>${{date}}</td><td>${{min}}</td><td>${{count}}</td><td>${{err}}</td>`;
          rows.appendChild(tr);
        }}
      }}

      document.getElementById('run').addEventListener('click', async () => {{
        const origin = document.getElementById('origin').value.trim();
        const dest = document.getElementById('dest').value.trim();
        const start = document.getElementById('start').value;
        const end = document.getElementById('end').value;
        const url = `/check?origin=${{encodeURIComponent(origin)}}&dest=${{encodeURIComponent(dest)}}&start=${{encodeURIComponent(start)}}&end=${{encodeURIComponent(end)}}`;
        out.textContent = `GET ${{url}}\\n\\nLoading...`;
        rows.innerHTML = '<tr><td colspan="4" class="muted">Loading...</td></tr>';
        try {{
          const resp = await fetch(url);
          const json = await resp.json();
          out.textContent = `GET ${{url}}\\nstatus=${{resp.status}}\\n\\n${{JSON.stringify(json, null, 2)}}`;
          setRows(json.results || []);
        }} catch (e) {{
          out.textContent = `Request failed: ${{e}}`;
          rows.innerHTML = `<tr><td colspan="4">Request failed: ${{e}}</td></tr>`;
        }}
      }});
    </script>
  </body>
</html>"""
    return HTMLResponse(content=html)


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
            payload = client.flight_offers_search(
                origin=origin.upper(),
                destination=dest.upper(),
                departure_date=day,
                adults=1,
                max_results=250,
            )
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

    return {
        "origin": origin.upper(),
        "dest": dest.upper(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "results": results,
    }
