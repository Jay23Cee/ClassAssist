# ClassAssist

ClassAssist is a lightweight Flask dashboard for managing live student help tickets backed by Google Sheets.

If you're coming from the **WillingWorkers** project style, this follows the same practical pattern: simple UI, sheet-backed data, and fast teacher workflows.

## What it does
- Polls a Google Sheet on an interval.
- Shows active tickets (`OPEN`, `IN_PROGRESS`) in a classroom dashboard.
- Lets a teacher claim, reopen, resolve, or mark no-show.
- Supports optional passcode protection for write actions.
- Suggests the next ticket using a wait-time-first queue.

## Tech stack
- Python 3 + Flask
- Google Sheets API (`google-api-python-client`)
- Plain HTML/CSS/JS frontend (no build step)

## Project structure
- `app.py` – Flask app and API routes.
- `poller.py` – sheet polling, queueing, action updates.
- `google_sheets.py` – authenticated Sheets client.
- `config.py` – app paths + config loading.
- `templates/dashboard.html` – dashboard UI.

## Prerequisites
- Python 3.10+ recommended
- A Google Cloud service account with access to your spreadsheet
- A spreadsheet tab with expected ticket columns (see below)

## Setup
1. **Clone and enter the project**
   ```bash
   git clone <your-repo-url>
   cd ClassAssist
   ```


2. **Create virtual environment and install dependencies**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt

   j
   ```

3. **Add service account credentials**
   - Place your service account JSON at:
     - `secrets/service_account.json`

4. **Create `config.json` in repo root**
   ```json
   {
     "sheet_id": "YOUR_SPREADSHEET_ID",
     "worksheet_name": "Form Responses 1",
     "poll_seconds": 30,
     "teacher_name": "Ms. Rivera",
     "teacher_passcode": "optional-passcode",
     "port": 5000
   }
   ```

## Expected sheet columns
ClassAssist reads and/or writes these headers (case/spacing normalized):

- Core read fields:
  - `Student`
  - `Period`
  - `Status`
  - `TicketId`
  - `Timestamp`
  - `Help Type`
  - `ClaimedBy`
  - `ClaimedAt`

- Action/update fields (used when applying actions):
  - `LastUpdated`
  - `NoShowAt`, `NoShowBy`
  - `ResolvedAt`, `ResolvedBy`
  - `TeacherTags`, `TagsAt`
  - `FollowUp`, `FollowUpAt`

## Run locally
```bash
python app.py
```

Then open:
- `http://127.0.0.1:5000`

## API overview
- `GET /api/tickets` – current tickets + metadata.
- `POST /api/action` – mutate ticket state.
  - actions: `claim`, `reopen`, `resolve`, `no_show`
- `GET /api/suggest` – suggest next ticket (filters optional).

## Auth behavior
- If `teacher_passcode` (or `admin_token`) is set in config, write routes require a matching token.
- Token is accepted in:
  - `X-Auth-Token` header,
  - query param `token`, or
  - JSON body field `token`.

## Troubleshooting
- **`Missing config.json`**: add `config.json` in project root.
- **`Missing service_account.json`**: create `secrets/service_account.json`.
- **No tickets shown**:
  - verify sheet ID + worksheet name,
  - ensure service account has access,
  - verify required headers exist.

## Notes
- Auto-refresh can be paused from the dashboard.
- Queue prioritizes longest waiting tickets.
- Only `OPEN` and `IN_PROGRESS` are displayed in the board.