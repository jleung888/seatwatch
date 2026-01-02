#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import sys
import time
from typing import Any, Callable, Optional

import requests


AMADEUS_DEFAULT_HOST = "https://test.api.amadeus.com"

_DEFAULT_AMADEUS_CLIENT: Optional["AmadeusClient"] = None


def _format_http_error(resp: requests.Response) -> str:
    try:
        payload = resp.json()
        body = json.dumps(payload, indent=2, ensure_ascii=False)
    except Exception:
        body = resp.text or ""
    if len(body) > 4000:
        body = body[:4000] + "\n...(truncated)..."
    return f"HTTP {resp.status_code} from {resp.url}\n{body}"


def compute_tightness(offer: dict) -> dict:
    """
    Returns:
      {
        "min_bookable": int|None,     # minimum numberOfBookableSeats seen in offer; 9 means '9+ / unknown'
        "score": int,                # 0-100
        "label": str                 # VERY TIGHT / TIGHT / MODERATE / UNKNOWN/LOOSE / NO DATA
      }
    """

    values: list[int] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "numberOfBookableSeats" and v is not None:
                    try:
                        values.append(int(v))
                    except Exception:
                        pass
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(offer)
    if not values:
        return {"min_bookable": None, "score": 0, "label": "NO DATA"}

    raw_min = min(values)
    min_bookable = 9 if raw_min >= 9 else raw_min

    if 1 <= min_bookable <= 8:
        score = 100 - (min_bookable - 1) * 12
    elif min_bookable == 9:
        score = 10
    else:
        score = 0

    if min_bookable <= 3:
        label = "VERY TIGHT"
    elif 4 <= min_bookable <= 6:
        label = "TIGHT"
    elif 7 <= min_bookable <= 8:
        label = "MODERATE"
    else:
        label = "UNKNOWN/LOOSE"

    return {"min_bookable": min_bookable, "score": int(score), "label": label}


class AmadeusClient:
    def __init__(self, client_id: str, client_secret: str, host: str = AMADEUS_DEFAULT_HOST) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._host = host.rstrip("/")
        self._session = requests.Session()
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def _ensure_token(self) -> str:
        now = time.time()
        if self._access_token and now < (self._token_expires_at - 30):
            return self._access_token

        url = f"{self._host}/v1/security/oauth2/token"
        resp = self._session.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=30,
        )
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            msg = _format_http_error(resp)
            msg += (
                "\n\nTips:\n"
                "- Verify AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET\n"
                "- Ensure AMADEUS_HOST matches your credentials environment (test vs production)\n"
            )
            raise requests.HTTPError(msg, response=resp) from exc
        payload = resp.json()
        token = payload.get("access_token")
        expires_in = payload.get("expires_in", 0)
        if not token:
            raise RuntimeError(f"Amadeus token response missing access_token: {payload}")

        self._access_token = str(token)
        try:
            self._token_expires_at = now + float(expires_in)
        except Exception:
            self._token_expires_at = now + 0.0
        return self._access_token

    def flight_offers_search(
        self,
        origin: str,
        destination: str,
        departure_date: dt.date,
        *,
        adults: int = 1,
        max_results: int = 250,
        travel_class: Optional[str] = None,
        non_stop: bool = False,
    ) -> dict[str, Any]:
        token = self._ensure_token()
        url = f"{self._host}/v2/shopping/flight-offers"
        params = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": departure_date.isoformat(),
            "adults": adults,
            "max": max_results,
        }
        if travel_class:
            params["travelClass"] = travel_class
        if non_stop:
            params["nonStop"] = "true"
        headers = {"Authorization": f"Bearer {token}"}

        resp = self._session.get(url, params=params, headers=headers, timeout=60)
        if resp.status_code == 401:
            self._access_token = None
            token = self._ensure_token()
            headers = {"Authorization": f"Bearer {token}"}
            resp = self._session.get(url, params=params, headers=headers, timeout=60)
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            if resp.status_code == 400:
                raise requests.HTTPError(_format_http_error(resp), response=resp) from exc
            raise
        return resp.json()

    def seatmaps(self, flight_offer: dict[str, Any]) -> dict[str, Any]:
        token = self._ensure_token()
        url = f"{self._host}/v1/shopping/seatmaps"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        payload_variants = [{"data": [flight_offer]}, {"data": {"flightOffers": [flight_offer]}}]
        last_exc: Optional[Exception] = None
        for payload in payload_variants:
            try:
                resp = self._session.post(url, json=payload, headers=headers, timeout=90)
                if resp.status_code == 401:
                    self._access_token = None
                    token = self._ensure_token()
                    headers["Authorization"] = f"Bearer {token}"
                    resp = self._session.post(url, json=payload, headers=headers, timeout=90)
                try:
                    resp.raise_for_status()
                except requests.HTTPError as exc:
                    if resp.status_code == 400:
                        raise requests.HTTPError(_format_http_error(resp), response=resp) from exc
                    raise
                return resp.json()
            except Exception as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc


def get_selectable_seat_counts(offer: dict, traveler_id: str = "1") -> dict:
    """
    Count selectable seats from the Amadeus Seatmaps API for a single Flight Offers Search offer.

    Returns:
      {
        "total_selectable": <int>,
        "selectable_by_cabin": { "<cabin>": <int>, ... }
      }
    """
    global _DEFAULT_AMADEUS_CLIENT
    client = _DEFAULT_AMADEUS_CLIENT
    if client is None:
        client_id = os.getenv("AMADEUS_CLIENT_ID", "")
        client_secret = os.getenv("AMADEUS_CLIENT_SECRET", "")
        host = os.getenv("AMADEUS_HOST", AMADEUS_DEFAULT_HOST)
        if not client_id or not client_secret:
            print("Seatmap count skipped: missing AMADEUS_CLIENT_ID/AMADEUS_CLIENT_SECRET", file=sys.stderr)
            return {"total_selectable": 0, "selectable_by_cabin": {}}
        client = AmadeusClient(client_id=client_id, client_secret=client_secret, host=host)
        _DEFAULT_AMADEUS_CLIENT = client

    url = f"{AMADEUS_DEFAULT_HOST}/v1/shopping/seatmaps"
    headers = {"Authorization": f"Bearer {client._ensure_token()}", "Content-Type": "application/json"}  # type: ignore[attr-defined]

    try:
        resp = client._session.post(url, json={"data": [offer]}, headers=headers, timeout=90)  # type: ignore[attr-defined]
        if resp.status_code == 401:
            client._access_token = None  # type: ignore[attr-defined]
            headers["Authorization"] = f"Bearer {client._ensure_token()}"  # type: ignore[attr-defined]
            resp = client._session.post(url, json={"data": [offer]}, headers=headers, timeout=90)  # type: ignore[attr-defined]
        if resp.status_code >= 400:
            print(f"Seatmap API error (HTTP {resp.status_code}) for {url}", file=sys.stderr)
            try:
                print(resp.text, file=sys.stderr)
            except Exception:
                pass
            return {"total_selectable": 0, "selectable_by_cabin": {}}
        payload = resp.json()
    except Exception as exc:
        print(f"Seatmap API call failed: {exc}", file=sys.stderr)
        return {"total_selectable": 0, "selectable_by_cabin": {}}

    total = 0
    by_cabin: dict[str, int] = {}

    def traveler_matches(seat: dict[str, Any]) -> bool:
        tp = seat.get("travelerPricing")
        if isinstance(tp, dict):
            tp = [tp]
        if not isinstance(tp, list):
            return False
        for entry in tp:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("travelerId")) != str(traveler_id):
                continue
            if str(entry.get("seatAvailabilityStatus", "")).upper() == "AVAILABLE":
                return True
        return False

    def walk(node: object) -> None:
        nonlocal total
        if isinstance(node, dict):
            if "travelerPricing" in node:
                seat = node
                if traveler_matches(seat):
                    cabin = seat.get("cabin") or seat.get("cabinType") or "UNKNOWN"
                    cabin_s = str(cabin)
                    total += 1
                    by_cabin[cabin_s] = by_cabin.get(cabin_s, 0) + 1
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    data = payload.get("data")
    walk(data)

    return {"total_selectable": total, "selectable_by_cabin": by_cabin}


def _env_or_arg(value: Optional[str], env_name: str) -> Optional[str]:
    return value if value not in (None, "") else os.getenv(env_name)


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid date {value!r}; expected YYYY-MM-DD") from exc


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _iter_dates(start: dt.date, end: dt.date):
    if end < start:
        raise ValueError("END_DATE must be >= START_DATE")
    cur = start
    one = dt.timedelta(days=1)
    while cur <= end:
        yield cur
        cur += one


def _is_direct_offer(offer: dict[str, Any]) -> bool:
    itineraries = offer.get("itineraries") or []
    if not isinstance(itineraries, list) or not itineraries:
        return False
    for it in itineraries:
        if not isinstance(it, dict):
            return False
        segments = it.get("segments") or []
        if not isinstance(segments, list) or len(segments) != 1:
            return False
    return True


def _offer_carrier_code(offer: dict[str, Any]) -> Optional[str]:
    itineraries = offer.get("itineraries") or []
    if not isinstance(itineraries, list) or not itineraries:
        return None
    first_it = itineraries[0]
    if not isinstance(first_it, dict):
        return None
    segments = first_it.get("segments") or []
    if not isinstance(segments, list) or not segments:
        return None
    seg0 = segments[0]
    if not isinstance(seg0, dict):
        return None
    code = seg0.get("carrierCode") or (seg0.get("operating") or {}).get("carrierCode")
    if not code:
        return None
    return str(code)


def _offer_operating_carrier_code(offer: dict[str, Any]) -> Optional[str]:
    itineraries = offer.get("itineraries") or []
    if not isinstance(itineraries, list) or not itineraries:
        return None
    first_it = itineraries[0]
    if not isinstance(first_it, dict):
        return None
    segments = first_it.get("segments") or []
    if not isinstance(segments, list) or not segments:
        return None
    seg0 = segments[0]
    if not isinstance(seg0, dict):
        return None
    operating = seg0.get("operating")
    if isinstance(operating, dict) and operating.get("carrierCode"):
        return str(operating.get("carrierCode"))
    if seg0.get("carrierCode"):
        return str(seg0.get("carrierCode"))
    return None


def _carriers_dictionary(amadeus_payload: dict[str, Any]) -> dict[str, str]:
    dictionaries = amadeus_payload.get("dictionaries") or {}
    if not isinstance(dictionaries, dict):
        return {}
    carriers = dictionaries.get("carriers") or {}
    if not isinstance(carriers, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in carriers.items():
        if k and v:
            out[str(k)] = str(v)
    return out


_CABINS = ("ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST")


def _departure_time_hhmm(departure_at: str) -> str:
    if "T" not in departure_at:
        return departure_at
    t = departure_at.split("T", 1)[1]
    return t[:5] if len(t) >= 5 else t


def _seatmap_available_seats_total(seatmaps_payload: dict[str, Any]) -> tuple[Optional[int], bool, int]:
    total = 0
    saw_any = False

    def is_available(value: object) -> bool:
        if value is True:
            return True
        if value is None:
            return False
        s = str(value).upper()
        return s == "AVAILABLE" or s.startswith("AVAILABLE") or s in {"OPEN", "FREE"}

    def walk(node: object) -> None:
        nonlocal total, saw_any
        if isinstance(node, dict):
            availability = (
                node.get("availabilityType")
                if node.get("availabilityType") is not None
                else node.get("availabilityStatus")
                if node.get("availabilityStatus") is not None
                else node.get("availability")
                if node.get("availability") is not None
                else node.get("available")
            )
            if availability is not None:
                saw_any = True
                if is_available(availability):
                    total += 1
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    data = seatmaps_payload.get("data")
    data_len = len(data) if isinstance(data, list) else (1 if data is not None else 0)
    walk(data)
    return (total if saw_any else None), saw_any, data_len


def _selectable_seat_counts_from_seatmaps_payload(seatmaps_payload: dict[str, Any], traveler_id: str = "1") -> dict:
    total = 0
    by_cabin: dict[str, int] = {}

    def norm_cabin(value: object) -> str:
        s = str(value).strip().upper().replace(" ", "_")
        return s if s else "UNKNOWN"

    def traveler_has_available(tp: object) -> bool:
        entries = tp
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("travelerId")) != str(traveler_id):
                continue
            if str(entry.get("seatAvailabilityStatus", "")).upper() == "AVAILABLE":
                return True
        return False

    def walk(node: object, cabin_ctx: Optional[str]) -> None:
        nonlocal total
        if isinstance(node, dict):
            cabin = cabin_ctx
            if node.get("cabin") is not None:
                cabin = norm_cabin(node.get("cabin"))

            if "travelerPricing" in node and traveler_has_available(node.get("travelerPricing")):
                cabin_final = cabin or norm_cabin(node.get("cabinType") or "UNKNOWN")
                total += 1
                by_cabin[cabin_final] = by_cabin.get(cabin_final, 0) + 1

            for v in node.values():
                walk(v, cabin)
        elif isinstance(node, list):
            for item in node:
                walk(item, cabin_ctx)

    walk(seatmaps_payload.get("data"), None)
    return {"total_selectable": total, "selectable_by_cabin": by_cabin}


def _offer_cabins(offer: dict[str, Any]) -> set[str]:
    cabins: set[str] = set()
    traveler_pricings = offer.get("travelerPricings") or []
    if not isinstance(traveler_pricings, list):
        return cabins
    for tp in traveler_pricings:
        if not isinstance(tp, dict):
            continue
        fare_details = tp.get("fareDetailsBySegment") or []
        if not isinstance(fare_details, list):
            continue
        for fd in fare_details:
            if not isinstance(fd, dict):
                continue
            cabin = fd.get("cabin")
            if cabin is None:
                continue
            cabins.add(str(cabin))
    return cabins


def find_direct_flight_max_bookable_seats_by_cabin(
    *,
    origin: str,
    destination: str,
    start_date: dt.date,
    end_date: dt.date,
    client_id: str,
    client_secret: str,
    host: str = AMADEUS_DEFAULT_HOST,
    adults: int = 1,
    max_results: int = 250,
    retries: int = 5,
    backoff_initial_s: float = 1.0,
    on_date: Optional[Callable[[dt.date, int, int], None]] = None,
    include_seatmaps: bool = False,
    debug_offers: bool = False,
    include_tightness: bool = False,
) -> list[dict[str, object]]:
    client = AmadeusClient(client_id=client_id, client_secret=client_secret, host=host)
    rows: list[dict[str, object]] = []

    total_days = (end_date - start_date).days + 1
    for idx, day in enumerate(_iter_dates(start_date, end_date)):
        if on_date is not None:
            on_date(day, idx, total_days)

        per_flight: dict[tuple[str, str], dict[str, object]] = {}
        carriers_for_day: dict[str, str] = {}
        seatmap_cache: dict[str, tuple[dict, str, Optional[str]]] = {}
        debug_seen_flights: set[str] = set()

        for cabin in _CABINS:
            backoff = backoff_initial_s
            for attempt in range(retries + 1):
                try:
                    payload = client.flight_offers_search(
                        origin=origin,
                        destination=destination,
                        departure_date=day,
                        adults=adults,
                        max_results=max_results,
                        travel_class=cabin,
                        non_stop=True,
                    )

                    carriers_for_day.update(_carriers_dictionary(payload))
                    offers = payload.get("data") or []
                    if not isinstance(offers, list):
                        offers = []

                    for offer in offers:
                        if not isinstance(offer, dict):
                            continue

                        itineraries = offer.get("itineraries") or []
                        first_it = itineraries[0] if isinstance(itineraries, list) and itineraries else None
                        if not isinstance(first_it, dict):
                            continue
                        segments = first_it.get("segments") or []
                        if not isinstance(segments, list) or not segments:
                            continue

                        if debug_offers:
                            for seg in segments:
                                if not isinstance(seg, dict):
                                    continue
                                op = None
                                operating = seg.get("operating")
                                if isinstance(operating, dict) and operating.get("carrierCode"):
                                    op = str(operating.get("carrierCode"))
                                elif seg.get("carrierCode"):
                                    op = str(seg.get("carrierCode"))
                                num = seg.get("number")
                                dep = seg.get("departure")
                                dep_at = dep.get("at") if isinstance(dep, dict) else None
                                if op and num and dep_at:
                                    debug_seen_flights.add(f"{op}{num}@{dep_at}")

                        if not _is_direct_offer(offer):
                            continue

                        operating_code = _offer_operating_carrier_code(offer)
                        if not operating_code:
                            continue

                        seg0 = segments[0]
                        if not isinstance(seg0, dict):
                            continue

                        flight_number = seg0.get("number")
                        if flight_number is None:
                            continue
                        flight_id = f"{operating_code}{flight_number}"

                        dep = seg0.get("departure")
                        departure_at = dep.get("at") if isinstance(dep, dict) else None
                        if not departure_at:
                            continue

                        offer_cabins = _offer_cabins(offer)
                        if offer_cabins and cabin not in offer_cabins:
                            continue

                        seats = offer.get("numberOfBookableSeats")
                        seats_int: Optional[int] = None
                        if seats is not None:
                            try:
                                seats_int = int(seats)
                            except Exception:
                                seats_int = None

                        key = (operating_code, f"{flight_id}@{departure_at}")
                        row = per_flight.get(key)
                        if row is None:
                            row = {
                                "date": day,
                                "airlineCode": operating_code,
                                "airline": carriers_for_day.get(operating_code, operating_code),
                                "flight": flight_id,
                                "departureAt": str(departure_at),
                                "departureTime": _departure_time_hhmm(str(departure_at)),
                                "economyMax": None,
                                "premiumEconomyMax": None,
                                "businessMax": None,
                                "firstMax": None,
                                "economySeen": False,
                                "premiumEconomySeen": False,
                                "businessSeen": False,
                                "firstSeen": False,
                                "seatmapSeen": False,
                                "tightnessSeen": False,
                                "tightnessMinBookable": None,
                                "tightnessScore": None,
                                "tightnessLabel": None,
                            }
                            per_flight[key] = row
                        else:
                            row["airline"] = carriers_for_day.get(operating_code, operating_code)

                        def upd(max_key: str, seen_key: str) -> None:
                            row[seen_key] = True
                            if seats_int is None:
                                return
                            cur = row.get(max_key)
                            if cur is None or (isinstance(cur, int) and seats_int > cur):
                                row[max_key] = seats_int

                        if cabin == "ECONOMY":
                            upd("economyMax", "economySeen")
                        elif cabin == "PREMIUM_ECONOMY":
                            upd("premiumEconomyMax", "premiumEconomySeen")
                        elif cabin == "BUSINESS":
                            upd("businessMax", "businessSeen")
                        elif cabin == "FIRST":
                            upd("firstMax", "firstSeen")

                        if include_tightness:
                            t = compute_tightness(offer)
                            mb = t.get("min_bookable")
                            if isinstance(mb, int):
                                cur_mb = row.get("tightnessMinBookable")
                                if (cur_mb is None) or (isinstance(cur_mb, int) and mb < cur_mb):
                                    row["tightnessMinBookable"] = mb
                                    row["tightnessScore"] = int(t.get("score", 0))
                                    row["tightnessLabel"] = str(t.get("label", "NO DATA"))
                                    row["tightnessSeen"] = True
                            else:
                                if not row.get("tightnessSeen"):
                                    row["tightnessSeen"] = True
                                    row["tightnessMinBookable"] = None
                                    row["tightnessScore"] = 0
                                    row["tightnessLabel"] = "NO DATA"

                        if include_seatmaps:
                            flight_key = key[1]
                            if flight_key not in seatmap_cache:
                                try:
                                    seatmaps_payload = client.seatmaps(offer)
                                    counts = _selectable_seat_counts_from_seatmaps_payload(seatmaps_payload, traveler_id="1")
                                    seatmap_cache[flight_key] = (counts, "ok", None)
                                except Exception as exc:
                                    seatmap_cache[flight_key] = ({"total_selectable": 0, "selectable_by_cabin": {}}, "error", str(exc))
                            row["seatmapSeen"] = True
                            counts, status, err = seatmap_cache.get(
                                flight_key, ({"total_selectable": 0, "selectable_by_cabin": {}}, "unknown", None)
                            )
                            row["seatmapStatus"] = status
                            row["seatmapError"] = err

                            if status == "ok":
                                by_cabin = counts.get("selectable_by_cabin") if isinstance(counts, dict) else {}
                                if not isinstance(by_cabin, dict):
                                    by_cabin = {}
                                row["selectableEconomy"] = int(by_cabin.get("ECONOMY", 0))
                                row["selectablePremiumEconomy"] = int(by_cabin.get("PREMIUM_ECONOMY", 0))
                                row["selectableBusiness"] = int(by_cabin.get("BUSINESS", 0))
                                row["selectableFirst"] = int(by_cabin.get("FIRST", 0))
                            else:
                                row["selectableEconomy"] = None
                                row["selectablePremiumEconomy"] = None
                                row["selectableBusiness"] = None
                                row["selectableFirst"] = None

                    break
                except requests.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else None
                    if status == 429 and attempt < retries:
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 16.0)
                        continue
                    raise

        for _key, row in sorted(per_flight.items(), key=lambda kv: (str(kv[1].get("airlineCode", "")), str(kv[1].get("departureAt", "")))):
            rows.append(row)
        if debug_offers:
            if debug_seen_flights:
                print(f"DEBUG {day.isoformat()} offers: {', '.join(sorted(debug_seen_flights))}", file=sys.stderr)
            else:
                print(f"DEBUG {day.isoformat()} offers: (none)", file=sys.stderr)

    return rows


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="List max numberOfBookableSeats by cabin (direct flights only), grouped by operating flight and date."
    )
    parser.add_argument("--origin", help="IATA origin airport/city code (or env ORIGIN)")
    parser.add_argument("--dest", help="IATA destination airport/city code (or env DEST)")
    parser.add_argument("--start-date", type=_parse_date, help="YYYY-MM-DD (or env START_DATE)")
    parser.add_argument("--end-date", type=_parse_date, help="YYYY-MM-DD (or env END_DATE)")
    parser.add_argument("--max", dest="max_results", type=int, default=_int_env("MAX_RESULTS", 250), help="Max offers (default: env MAX_RESULTS or 250)")
    parser.add_argument("--amadeus-host", default=os.getenv("AMADEUS_HOST", AMADEUS_DEFAULT_HOST), help="API host (default: env AMADEUS_HOST or test host)")
    parser.add_argument("--client-id", help="Amadeus client id (or env AMADEUS_CLIENT_ID)")
    parser.add_argument("--client-secret", help="Amadeus client secret (or env AMADEUS_CLIENT_SECRET)")
    parser.add_argument("--seatmaps", action="store_true", help="Also fetch seatmap availability (slower)")
    parser.add_argument("--seatmap-count", action="store_true", help="Count selectable seats on the first offer per date (prints to stdout)")
    parser.add_argument("--debug-offers", action="store_true", help="Debug: print all flight numbers returned per date (stderr)")
    parser.add_argument("--tightness", action="store_true", help="Append tightness columns to each flight row")
    args = parser.parse_args(argv)

    origin = _env_or_arg(args.origin, "ORIGIN")
    dest = _env_or_arg(args.dest, "DEST")
    start_date_env = os.getenv("START_DATE")
    end_date_env = os.getenv("END_DATE")
    try:
        start_date_raw = args.start_date or (start_date_env and dt.date.fromisoformat(start_date_env))
        end_date_raw = args.end_date or (end_date_env and dt.date.fromisoformat(end_date_env))
    except ValueError as exc:
        parser.error(str(exc))
    client_id = _env_or_arg(args.client_id, "AMADEUS_CLIENT_ID")
    client_secret = _env_or_arg(args.client_secret, "AMADEUS_CLIENT_SECRET")

    missing = [name for name, val in [("ORIGIN", origin), ("DEST", dest), ("START_DATE", start_date_raw), ("END_DATE", end_date_raw)] if not val]
    if missing:
        parser.error(f"Missing required inputs (env or args): {', '.join(missing)}")
    if not client_id or not client_secret:
        parser.error("Missing Amadeus credentials: set env AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET (or pass --client-id/--client-secret)")

    start_date = start_date_raw
    end_date = end_date_raw
    assert isinstance(start_date, dt.date)
    assert isinstance(end_date, dt.date)

    if start_date < dt.date.today():
        parser.error("START_DATE must be today or later (Amadeus rejects past departures).")

    global _DEFAULT_AMADEUS_CLIENT
    _DEFAULT_AMADEUS_CLIENT = AmadeusClient(client_id=client_id, client_secret=client_secret, host=args.amadeus_host)

    if args.seatmap_count:
        for day in _iter_dates(start_date, end_date):
            try:
                payload = _DEFAULT_AMADEUS_CLIENT.flight_offers_search(
                    origin=origin,
                    destination=dest,
                    departure_date=day,
                    max_results=1,
                    travel_class="ECONOMY",
                    non_stop=True,
                )
                offers = payload.get("data") or []
                if not isinstance(offers, list) or not offers:
                    print(f"{day.isoformat()}\tSEATMAP\t(no offers)")
                    continue
                offer = offers[0]
                if not isinstance(offer, dict):
                    print(f"{day.isoformat()}\tSEATMAP\t(no offer dict)")
                    continue
                counts = get_selectable_seat_counts(offer, traveler_id="1")
                by_cabin = counts.get("selectable_by_cabin") or {}
                print(f"{day.isoformat()}\tSEATMAP\ttotal_selectable={counts.get('total_selectable', 0)}")
                if isinstance(by_cabin, dict) and by_cabin:
                    for cabin, n in sorted(by_cabin.items(), key=lambda kv: str(kv[0])):
                        print(f"{day.isoformat()}\tSEATMAP\t{cabin}={n}")
            except Exception as exc:
                print(f"{day.isoformat()}\tSEATMAP\t(error: {exc})")

    rows = find_direct_flight_max_bookable_seats_by_cabin(
        origin=origin,
        destination=dest,
        start_date=start_date,
        end_date=end_date,
        client_id=client_id,
        client_secret=client_secret,
        host=args.amadeus_host,
        max_results=args.max_results,
        include_seatmaps=args.seatmaps,
        debug_offers=args.debug_offers,
        include_tightness=args.tightness,
    )
    for row in rows:
        day = row.get("date")
        airline = row.get("airline") or ""
        flight = row.get("flight") or ""
        departure_time = row.get("departureTime") or ""

        def fmt(max_key: str, seen_key: str) -> str:
            if not row.get(seen_key):
                return ""
            v = row.get(max_key)
            return "unknown" if v is None else str(v)

        economy = fmt("economyMax", "economySeen")
        prem = fmt("premiumEconomyMax", "premiumEconomySeen")
        business = fmt("businessMax", "businessSeen")
        first = fmt("firstMax", "firstSeen")
        sel_e = ""
        sel_pe = ""
        sel_b = ""
        sel_f = ""
        if row.get("seatmapSeen"):
            status = row.get("seatmapStatus") or "unknown"
            if status == "ok":
                sel_e = str(row.get("selectableEconomy", 0))
                sel_pe = str(row.get("selectablePremiumEconomy", 0))
                sel_b = str(row.get("selectableBusiness", 0))
                sel_f = str(row.get("selectableFirst", 0))
            else:
                sel_e = sel_pe = sel_b = sel_f = str(status)

        base = (
            f"{day.isoformat()}\t{airline}\t{flight}\t{departure_time}"
            f"\t{economy}\t{prem}\t{business}\t{first}"
            f"\t{sel_e}\t{sel_pe}\t{sel_b}\t{sel_f}"
        )
        if args.tightness:
            mb = row.get("tightnessMinBookable")
            mb_s = "None" if mb is None else str(mb)
            label = row.get("tightnessLabel") or "NO DATA"
            score = row.get("tightnessScore")
            score_s = "0" if score is None else str(score)
            print(f"{base}\tmin_bookable={mb_s}\ttightness={label}\tscore={score_s}")
        else:
            print(base)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
