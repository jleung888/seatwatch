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

from seats_watch import AMADEUS_DEFAULT_HOST, find_direct_flight_max_bookable_seats_by_cabin

app = FastAPI()


def _amadeus_creds() -> tuple[str, str]:
    client_id = os.getenv("AMADEUS_CLIENT_ID")
    client_secret = os.getenv("AMADEUS_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Missing AMADEUS_CLIENT_ID/AMADEUS_CLIENT_SECRET")
    return client_id, client_secret


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
      table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
      th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; white-space: nowrap; }}
      th {{ background: #f6f8fa; position: sticky; top: 0; }}
      .muted {{ color: #666; font-size: 12px; }}
      .scroll {{ overflow: auto; border: 1px solid #ddd; border-radius: 8px; max-height: 65vh; }}
    </style>
  </head>
  <body>
    <h1>Seatwatch</h1>
    <p class="muted">Direct flights grouped by operating flight and date. Tightness is included. Seatmaps are excluded.</p>

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

    <h3>Flights</h3>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>Date</th>
            <th>Airline</th>
            <th>Flight</th>
            <th>Departure</th>
            <th>Tightness</th>
            <th>Score</th>
            <th>Min bookable</th>
            <th>Economy max</th>
            <th>Prem Econ max</th>
            <th>Business max</th>
            <th>First max</th>
          </tr>
        </thead>
        <tbody id="rows">
          <tr><td colspan="11" class="muted">Click Run…</td></tr>
        </tbody>
      </table>
    </div>

    <h3>Raw JSON</h3>
    <pre id="out">(not run yet)</pre>

    <script>
      const out = document.getElementById('out');
      const rows = document.getElementById('rows');

      function cell(v) {{
        if (v === undefined || v === null) return '';
        return String(v);
      }}

      function setRows(items) {{
        rows.innerHTML = '';
        if (!items || items.length === 0) {{
          rows.innerHTML = '<tr><td colspan="11" class="muted">No results.</td></tr>';
          return;
        }}
        for (const r of items) {{
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td>${{cell(r.date)}}</td>
            <td>${{cell(r.airline)}}</td>
            <td>${{cell(r.flight)}}</td>
            <td>${{cell(r.departureTime)}}</td>
            <td>${{cell(r.tightnessLabel)}}</td>
            <td>${{cell(r.tightnessScore)}}</td>
            <td>${{cell(r.tightnessMinBookable)}}</td>
            <td>${{cell(r.economyMax)}}</td>
            <td>${{cell(r.premiumEconomyMax)}}</td>
            <td>${{cell(r.businessMax)}}</td>
            <td>${{cell(r.firstMax)}}</td>
          `;
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
        rows.innerHTML = '<tr><td colspan="11" class="muted">Loading...</td></tr>';
        try {{
          const resp = await fetch(url);
          const json = await resp.json();
          out.textContent = `GET ${{url}}\\nstatus=${{resp.status}}\\n\\n${{JSON.stringify(json, null, 2)}}`;
          setRows(json.rows || []);
        }} catch (e) {{
          out.textContent = `Request failed: ${{e}}`;
          rows.innerHTML = `<tr><td colspan="11">Request failed: ${{e}}</td></tr>`;
        }}
      }});
    </script>
  </body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/check")
def check(
    origin: str = Query(..., min_length=3, max_length=3),
    dest: str = Query(..., min_length=3, max_length=3),
    start: dt.date = Query(...),
    end: dt.date = Query(...),
) -> dict[str, Any]:
    if end < start:
        raise HTTPException(status_code=400, detail="end must be on/after start")

    client_id, client_secret = _amadeus_creds()
    rows = find_direct_flight_max_bookable_seats_by_cabin(
        origin=origin.upper(),
        destination=dest.upper(),
        start_date=start,
        end_date=end,
        client_id=client_id,
        client_secret=client_secret,
        host=AMADEUS_DEFAULT_HOST,
        max_results=250,
        include_seatmaps=False,
        include_tightness=True,
        debug_offers=False,
    )

    out_rows: list[dict[str, Any]] = []
    for row in rows:
        d = row.get("date")
        out_rows.append(
            {
                "date": d.isoformat() if hasattr(d, "isoformat") else str(d),
                "airline": row.get("airline"),
                "flight": row.get("flight"),
                "departureTime": row.get("departureTime"),
                "economyMax": row.get("economyMax"),
                "premiumEconomyMax": row.get("premiumEconomyMax"),
                "businessMax": row.get("businessMax"),
                "firstMax": row.get("firstMax"),
                "tightnessMinBookable": row.get("tightnessMinBookable"),
                "tightnessScore": row.get("tightnessScore"),
                "tightnessLabel": row.get("tightnessLabel"),
            }
        )

    return {
        "origin": origin.upper(),
        "dest": dest.upper(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rows": out_rows,
    }
