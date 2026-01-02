import datetime as dt
import os
import queue
import threading
import traceback
import tkinter as tk
from tkinter import ttk

from seats_watch import AMADEUS_DEFAULT_HOST, find_direct_flight_max_bookable_seats_by_cabin


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value.strip())


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Seat Watch")
        self.geometry("760x520")

        self._events: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._last_error_details = ""

        self._build_ui()
        self._poll_events()

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)

        ttk.Label(
            root,
            text="Show max numberOfBookableSeats by cabin (direct flights only), grouped by operating flight and date.",
            wraplength=720,
        ).grid(row=0, column=0, sticky="w")

        form = ttk.LabelFrame(root, text="Inputs", padding=10)
        form.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        for i in range(6):
            form.columnconfigure(i, weight=1)

        self.origin_var = tk.StringVar(value=_env("ORIGIN", ""))
        self.dest_var = tk.StringVar(value=_env("DEST", ""))
        self.start_var = tk.StringVar(value=_env("START_DATE", dt.date.today().isoformat()))
        self.end_var = tk.StringVar(value=_env("END_DATE", (dt.date.today() + dt.timedelta(days=14)).isoformat()))
        self.max_var = tk.StringVar(value=_env("MAX_RESULTS", "250"))
        self.seatmaps_var = tk.BooleanVar(value=_env("INCLUDE_SEATMAPS", "").strip().lower() in ("1", "true", "yes", "on"))

        self.host_var = tk.StringVar(value=_env("AMADEUS_HOST", AMADEUS_DEFAULT_HOST))
        self.client_id_var = tk.StringVar(value=_env("AMADEUS_CLIENT_ID", ""))
        self.client_secret_var = tk.StringVar(value=_env("AMADEUS_CLIENT_SECRET", ""))

        def labeled_entry(row: int, col: int, label: str, var: tk.StringVar, *, show: str | None = None) -> None:
            ttk.Label(form, text=label).grid(row=row, column=col, sticky="w")
            ent = ttk.Entry(form, textvariable=var, show=show or "")
            ent.grid(row=row + 1, column=col, sticky="ew", padx=(0, 8))

        labeled_entry(0, 0, "Origin (IATA)", self.origin_var)
        labeled_entry(0, 1, "Destination (IATA)", self.dest_var)
        labeled_entry(0, 2, "Max offers", self.max_var)

        labeled_entry(2, 0, "Start date (YYYY-MM-DD)", self.start_var)
        labeled_entry(2, 1, "End date (YYYY-MM-DD)", self.end_var)

        labeled_entry(4, 0, "Amadeus host", self.host_var)
        labeled_entry(4, 1, "Client ID", self.client_id_var)
        labeled_entry(4, 2, "Client Secret", self.client_secret_var, show="*")

        ttk.Checkbutton(form, text="Fetch selectable seat counts (seatmap, slower)", variable=self.seatmaps_var).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )

        actions = ttk.Frame(root)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        actions.columnconfigure(1, weight=1)

        self.run_btn = ttk.Button(actions, text="Run seat watch", command=self._on_run)
        self.run_btn.grid(row=0, column=0, sticky="w")

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(actions, textvariable=self.status_var).grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.progress = ttk.Progressbar(root, orient="horizontal", mode="determinate")
        self.progress.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        results = ttk.LabelFrame(root, text="Results", padding=10)
        results.grid(row=4, column=0, sticky="nsew")
        root.rowconfigure(4, weight=1)
        results.columnconfigure(0, weight=1)
        results.rowconfigure(0, weight=1)
        self._results_notebook = ttk.Notebook(results)
        self._results_notebook.grid(row=0, column=0, sticky="nsew")

        flights_frame = ttk.Frame(self._results_notebook)
        flights_frame.columnconfigure(0, weight=1)
        flights_frame.rowconfigure(0, weight=1)
        flights_frame.rowconfigure(1, weight=0)

        error_frame = ttk.Frame(self._results_notebook)
        error_frame.columnconfigure(0, weight=1)
        error_frame.rowconfigure(1, weight=1)

        self._results_notebook.add(flights_frame, text="Flights")
        self._results_notebook.add(error_frame, text="Error")

        self._results_container = flights_frame
        self._table_scrollbar: ttk.Scrollbar | None = None
        self._table_scrollbar_x: ttk.Scrollbar | None = None
        self.table: ttk.Treeview | None = None
        self._init_table()

        error_actions = ttk.Frame(error_frame)
        error_actions.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(error_actions, text="Copy error", command=self._copy_error).pack(side="left")
        ttk.Button(error_actions, text="Select all", command=self._select_all_error).pack(side="left", padx=(8, 0))
        ttk.Button(error_actions, text="Clear", command=self._clear_error).pack(side="left", padx=(8, 0))

        self._error_text = tk.Text(error_frame, wrap="none", height=10)
        self._error_text.grid(row=1, column=0, sticky="nsew")

        error_scroll_y = ttk.Scrollbar(error_frame, orient="vertical", command=self._error_text.yview)
        error_scroll_y.grid(row=1, column=1, sticky="ns")
        self._error_text.configure(yscrollcommand=error_scroll_y.set)

        error_scroll_x = ttk.Scrollbar(error_frame, orient="horizontal", command=self._error_text.xview)
        error_scroll_x.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self._error_text.configure(xscrollcommand=error_scroll_x.set)
        self._error_text.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        self.run_btn.configure(state=("disabled" if running else "normal"))

    def _init_table(self) -> None:
        if self.table is not None:
            self.table.destroy()
            self.table = None
        if self._table_scrollbar is not None:
            self._table_scrollbar.destroy()
            self._table_scrollbar = None
        if self._table_scrollbar_x is not None:
            self._table_scrollbar_x.destroy()
            self._table_scrollbar_x = None

        columns = (
            "date",
            "airline",
            "flight",
            "departure",
            "tightness",
            "score",
            "min_bookable",
            "economy",
            "premium",
            "business",
            "first",
            "sel_e",
            "sel_pe",
            "sel_b",
            "sel_f",
        )
        self.table = ttk.Treeview(self._results_container, columns=columns, show="headings", selectmode="browse")
        self.table.heading("date", text="Date")
        self.table.heading("airline", text="Airline")
        self.table.heading("flight", text="Flight")
        self.table.heading("departure", text="Departure")
        self.table.heading("tightness", text="Tightness")
        self.table.heading("score", text="Score")
        self.table.heading("min_bookable", text="Min bookable")
        self.table.heading("economy", text="Economy (max)")
        self.table.heading("premium", text="Premium Econ (max)")
        self.table.heading("business", text="Business (max)")
        self.table.heading("first", text="First (max)")
        self.table.heading("sel_e", text="Selectable Econ")
        self.table.heading("sel_pe", text="Selectable Prem")
        self.table.heading("sel_b", text="Selectable Bus")
        self.table.heading("sel_f", text="Selectable First")
        self.table.column("date", width=110, anchor="w")
        self.table.column("airline", width=210, anchor="w")
        self.table.column("flight", width=80, anchor="w")
        self.table.column("departure", width=170, anchor="w")
        self.table.column("tightness", width=110, anchor="w")
        self.table.column("score", width=80, anchor="w")
        self.table.column("min_bookable", width=110, anchor="w")
        self.table.column("economy", width=110, anchor="w")
        self.table.column("premium", width=140, anchor="w")
        self.table.column("business", width=120, anchor="w")
        self.table.column("first", width=110, anchor="w")
        self.table.column("sel_e", width=120, anchor="w")
        self.table.column("sel_pe", width=120, anchor="w")
        self.table.column("sel_b", width=120, anchor="w")
        self.table.column("sel_f", width=120, anchor="w")

        assert self.table is not None
        self.table.grid(row=0, column=0, sticky="nsew")
        self._table_scrollbar = ttk.Scrollbar(self._results_container, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=self._table_scrollbar.set)
        self._table_scrollbar.grid(row=0, column=1, sticky="ns")

        self._table_scrollbar_x = ttk.Scrollbar(self._results_container, orient="horizontal", command=self.table.xview)
        self.table.configure(xscrollcommand=self._table_scrollbar_x.set)
        self._table_scrollbar_x.grid(row=1, column=0, sticky="ew", pady=(6, 0))

    def _set_error(self, details: str) -> None:
        self._last_error_details = details
        self.status_var.set("Error (see Error tab).")
        self._results_notebook.select(1)
        self._error_text.configure(state="normal")
        self._error_text.delete("1.0", "end")
        self._error_text.insert("1.0", details)
        self._error_text.configure(state="disabled")
        self._select_all_error()

    def _clear_error(self) -> None:
        self._last_error_details = ""
        self._error_text.configure(state="normal")
        self._error_text.delete("1.0", "end")
        self._error_text.configure(state="disabled")

    def _select_all_error(self) -> None:
        try:
            self._error_text.tag_add("sel", "1.0", "end-1c")
            self._error_text.mark_set("insert", "1.0")
            self._error_text.see("1.0")
            self._error_text.focus_set()
        except Exception:
            pass

    def _copy_error(self) -> None:
        if not self._last_error_details:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(self._last_error_details)
        except Exception:
            pass

    def _clear_results(self) -> None:
        self._clear_error()
        if self.table is None:
            return
        for item in self.table.get_children():
            self.table.delete(item)

    def _on_run(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return

        origin = self.origin_var.get().strip().upper()
        dest = self.dest_var.get().strip().upper()
        host = self.host_var.get().strip()
        client_id = self.client_id_var.get().strip()
        client_secret = self.client_secret_var.get().strip()

        if not origin or not dest:
            self._events.put(("error", "Origin and destination are required."))
            return
        if not client_id or not client_secret:
            self._events.put(("error", "Client ID and Client Secret are required."))
            return

        try:
            start_date = _parse_date(self.start_var.get())
            end_date = _parse_date(self.end_var.get())
            max_results = int(self.max_var.get().strip())
        except Exception as exc:
            self._events.put(("error", f"Invalid input: {exc}"))
            return

        if start_date < dt.date.today():
            self._events.put(("error", "Start date must be today or later (Amadeus rejects past departures)."))
            return

        if end_date < start_date:
            self._events.put(("error", "End date must be on/after start date."))
            return

        self._clear_results()
        self.progress.configure(value=0)
        self.status_var.set("Starting...")
        self._set_running(True)

        def worker() -> None:
            try:
                def on_date(day: dt.date, idx: int, total: int) -> None:
                    self._events.put(("progress", (day, idx, total)))

                rows = find_direct_flight_max_bookable_seats_by_cabin(
                    origin=origin,
                    destination=dest,
                    start_date=start_date,
                    end_date=end_date,
                    client_id=client_id,
                    client_secret=client_secret,
                    host=host or AMADEUS_DEFAULT_HOST,
                    max_results=max_results,
                    on_date=on_date,
                    include_seatmaps=bool(self.seatmaps_var.get()),
                    include_tightness=True,
                )
                self._events.put(("done_rows", rows))
            except Exception as exc:
                details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                self._events.put(("exception", details))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "progress":
                    day, idx, total = payload  # type: ignore[misc]
                    self.status_var.set(f"Checking {day.isoformat()} ({idx + 1}/{total})...")
                    self.progress.configure(maximum=max(total, 1), value=idx + 1)
                elif kind == "done_rows":
                    self._results_notebook.select(0)
                    self._clear_error()
                    rows = payload  # type: ignore[assignment]
                    assert self.table is not None
                    for row in rows:
                        day = row.get("date")
                        airline = row.get("airline") or ""
                        flight = row.get("flight") or ""
                        departure_time = row.get("departureTime") or (row.get("departureAt") or "")

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

                        mb = row.get("tightnessMinBookable")
                        mb_s = "" if mb is None else str(mb)
                        label = row.get("tightnessLabel") or ""
                        score = row.get("tightnessScore")
                        score_s = "" if score is None else str(score)
                        self.table.insert(
                            "",
                            "end",
                            values=(
                                day.isoformat(),
                                airline,
                                flight,
                                departure_time,
                                label,
                                score_s,
                                mb_s,
                                economy,
                                prem,
                                business,
                                first,
                                sel_e,
                                sel_pe,
                                sel_b,
                                sel_f,
                            ),
                        )
                    self.status_var.set(f"Done. {len(rows)} flight(s).")
                    self._set_running(False)
                elif kind == "error":
                    self._set_error(str(payload))
                    self._set_running(False)
                elif kind == "exception":
                    self._set_error(str(payload))
                    self._set_running(False)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
