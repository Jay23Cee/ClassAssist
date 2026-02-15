import ssl
import threading
import time
from datetime import datetime

from google_sheets import build_sheets_service
from helpers import col_to_letter, normalize, now_str, safe_parse_timestamp
from logging_setup import get_logger


logger = get_logger()


class SheetPoller:
    def __init__(self, cfg):
        self.sheet_id = cfg["sheet_id"]
        self.sheet_name = cfg.get("worksheet_name", "Form Responses 1")
        self.interval = int(cfg.get("poll_seconds", 30))
        self.teacher = cfg.get("teacher_name", "Teacher")

        self.visible_statuses = {"OPEN", "IN_PROGRESS"}

        self._tickets = []
        self._meta = {"last_ok": None, "last_error": None}

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._service = None

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def get_state(self):
        with self._lock:
            return list(self._tickets), dict(self._meta)

    def _loop(self):
        while not self._stop.is_set():
            try:
                if not self._service:
                    self._service = build_sheets_service()

                tickets = self._read_sheet()

                with self._lock:
                    self._tickets = tickets
                    self._meta["last_ok"] = datetime.now().isoformat(timespec="seconds")
                    self._meta["last_error"] = None

            except Exception as e:
                logger.exception("Poll error")
                with self._lock:
                    self._meta["last_error"] = str(e)

            time.sleep(self.interval)

    def _safe_execute(self, request_obj, retries=2):
        last = None
        for attempt in range(int(retries)):
            try:
                return request_obj.execute()
            except ssl.SSLError as e:
                last = e
                logger.exception(
                    "SSL error during Google Sheets call (attempt %s/%s)",
                    attempt + 1,
                    retries,
                )
                try:
                    self._service = build_sheets_service()
                except Exception:
                    pass
                time.sleep(0.4 * (attempt + 1))
        raise last

    def _read_sheet(self):
        result = self._safe_execute(
            self._service.spreadsheets().values().get(
                spreadsheetId=self.sheet_id,
                range=f"{self.sheet_name}!A1:ZZ",
            )
        )

        values = result.get("values", [])
        if len(values) < 2:
            return []

        headers = values[0]
        idx = {normalize(h): i for i, h in enumerate(headers)}

        def get(row, name):
            i = idx.get(name)
            return "" if i is None or i >= len(row) else row[i]

        tickets = []
        seen = set()

        for r in range(1, len(values)):
            row = values[r]
            row_num = r + 1

            student = normalize(get(row, "Student"))
            period = normalize(get(row, "Period"))
            status = normalize(get(row, "Status")).upper()
            ticket_id = normalize(get(row, "TicketId"))
            ts = safe_parse_timestamp(get(row, "Timestamp"))

            if not status:
                status = "OPEN"

            if status not in self.visible_statuses:
                continue

            if not student or not period:
                continue

            if not ticket_id:
                ticket_id = f"ROW_{row_num}"

            key = (student.upper(),)
            if key in seen:
                continue
            seen.add(key)

            wait = None
            if ts:
                wait = int((datetime.now() - ts).total_seconds())

            tickets.append(
                {
                    "ticket_id": ticket_id,
                    "student": student,
                    "period": period,
                    "help_type": normalize(get(row, "Help Type")),
                    "timestamp": normalize(get(row, "Timestamp")),
                    "wait_seconds": wait,
                    "row": row_num,
                    "status": status,
                    "claimed_by": normalize(get(row, "ClaimedBy")),
                    "claimed_at": normalize(get(row, "ClaimedAt")),
                }
            )

        def sort_key(t):
            status_rank = 0 if t.get("status") == "OPEN" else 1
            ws = t.get("wait_seconds")
            ws_rank = -(ws if isinstance(ws, int) else -1)
            if ws is None:
                ws_rank = 10**12
            return (status_rank, ws_rank)

        tickets.sort(key=sort_key)
        return tickets

    def _get_sheet_data_and_cols(self):
        data = self._safe_execute(
            self._service.spreadsheets().values().get(
                spreadsheetId=self.sheet_id,
                range=f"{self.sheet_name}!A1:ZZ",
            )
        ).get("values", [])

        if len(data) < 2:
            return None, None

        headers = data[0]
        col = {normalize(h): i + 1 for i, h in enumerate(headers)}
        return data, col

    def _batch_update_cells(self, row_index_1based, col_map, updates_dict):
        body = {"valueInputOption": "RAW", "data": []}

        for col_name, val in updates_dict.items():
            if col_name not in col_map:
                continue
            cell = f"{col_to_letter(col_map[col_name])}{row_index_1based}"
            body["data"].append(
                {
                    "range": f"{self.sheet_name}!{cell}",
                    "values": [[val]],
                }
            )

        if not body["data"]:
            return False, "NO_VALID_COLUMNS"

        self._safe_execute(
            self._service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.sheet_id,
                body=body,
            )
        )

        try:
            row_vals = self._safe_execute(
                self._service.spreadsheets().values().get(
                    spreadsheetId=self.sheet_id,
                    range=f"{self.sheet_name}!A{row_index_1based}:ZZ{row_index_1based}",
                )
            ).get("values", [])

            if not row_vals:
                return False, "NOT_CONFIRMED"

            row = row_vals[0]

            def read_cell(col_name):
                idx_1 = col_map.get(col_name)
                if not idx_1:
                    return None
                i0 = idx_1 - 1
                if i0 >= len(row):
                    return ""
                return str(row[i0]).strip()

            for col_name, expected in updates_dict.items():
                if col_name not in col_map:
                    continue
                got = read_cell(col_name)
                exp = str(expected).strip()
                if got != exp:
                    return False, f"VERIFY_MISMATCH_{col_name}"

            return True, "OK"

        except Exception:
            logger.exception("Verify error")
            return False, "NOT_CONFIRMED"

    def _update_cache_ticket(self, ticket_id, patch):
        with self._lock:
            for t in self._tickets:
                if normalize(t.get("ticket_id")) == normalize(ticket_id):
                    t.update(patch)
                    break

            status = normalize(patch.get("status")).upper() if patch.get("status") else ""
            if status and status not in self.visible_statuses:
                self._tickets = [
                    t
                    for t in self._tickets
                    if normalize(t.get("ticket_id")) != normalize(ticket_id)
                ]

            self._meta["last_ok"] = datetime.now().isoformat(timespec="seconds")
            self._meta["last_error"] = None

    def apply_action(self, ticket_id, action, tags=""):
        action = normalize(action).lower()
        ticket_id = normalize(ticket_id)

        if not ticket_id:
            return False, "MISSING_TICKET_ID"

        data, col = self._get_sheet_data_and_cols()
        if data is None:
            return False, "NO_DATA"

        for needed in ["TicketId", "Status", "LastUpdated"]:
            if needed not in col:
                return False, f"Missing column {needed}"

        target_row_1based = None
        for i in range(1, len(data)):
            row = data[i]
            try:
                cell_val = row[col["TicketId"] - 1]
            except Exception:
                cell_val = ""
            if normalize(cell_val) == ticket_id:
                target_row_1based = i + 1
                break

        if not target_row_1based:
            return False, "NOT_FOUND"

        now = now_str()

        if action == "claim":
            updates = {
                "Status": "IN_PROGRESS",
                "ClaimedAt": now,
                "ClaimedBy": self.teacher,
                "LastUpdated": now,
            }
            ok, msg = self._batch_update_cells(target_row_1based, col, updates)
            if ok:
                self._update_cache_ticket(
                    ticket_id,
                    {
                        "status": "IN_PROGRESS",
                        "claimed_at": now,
                        "claimed_by": self.teacher,
                    },
                )
            return ok, msg

        if action == "reopen":
            updates = {
                "Status": "OPEN",
                "ClaimedAt": "",
                "ClaimedBy": "",
                "LastUpdated": now,
            }
            ok, msg = self._batch_update_cells(target_row_1based, col, updates)
            if ok:
                self._update_cache_ticket(
                    ticket_id,
                    {
                        "status": "OPEN",
                        "claimed_at": "",
                        "claimed_by": "",
                    },
                )
            return ok, msg

        if action == "no_show":
            updates = {
                "Status": "NO_SHOW",
                "NoShowAt": now,
                "NoShowBy": self.teacher,
                "LastUpdated": now,
            }
            ok, msg = self._batch_update_cells(target_row_1based, col, updates)
            if ok:
                self._update_cache_ticket(ticket_id, {"status": "NO_SHOW"})
            return ok, msg

        if action == "resolve":
            updates = {
                "Status": "RESOLVED",
                "ResolvedAt": now,
                "ResolvedBy": self.teacher,
                "LastUpdated": now,
            }
            t = normalize(tags)
            if t:
                updates["TeacherTags"] = t
                updates["TagsAt"] = now
                if "follow up" in t.lower():
                    updates["FollowUp"] = "YES"
                    updates["FollowUpAt"] = now
            ok, msg = self._batch_update_cells(target_row_1based, col, updates)
            if ok:
                self._update_cache_ticket(ticket_id, {"status": "RESOLVED"})
            return ok, msg

        return False, "BAD_ACTION"

    def suggest_next(self, period_filter="", help_type_filter="", status_filter="OPEN"):
        period_filter = normalize(period_filter)
        help_type_filter = normalize(help_type_filter)
        status_filter = normalize(status_filter).upper() or "OPEN"

        with self._lock:
            tickets = list(self._tickets)

        if status_filter:
            tickets = [
                t for t in tickets if normalize(t.get("status")).upper() == status_filter
            ]

        if period_filter:
            tickets = [t for t in tickets if normalize(t.get("period")) == period_filter]

        if help_type_filter:
            tickets = [
                t for t in tickets if normalize(t.get("help_type")) == help_type_filter
            ]

        if not tickets:
            return None

        def period_num(p):
            p = normalize(p)
            try:
                return int(p)
            except Exception:
                return 10**9

        def wait_val(w):
            return w if isinstance(w, int) else -1

        tickets.sort(
            key=lambda t: (
                -wait_val(t.get("wait_seconds")),
                period_num(t.get("period")),
                normalize(t.get("student")).lower(),
            )
        )
        return tickets[0]
