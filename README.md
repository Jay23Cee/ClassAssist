# ClassAssist

ClassAssist is a lightweight Flask dashboard designed to manage live student help requests submitted through a Google Form and stored in Google Sheets. It provides teachers with a clear, fair, and structured way to respond to students who need assistance, ensuring support is given in order rather than based on who calls out first.

---

## Purpose

In active classrooms, several students may request help at the same time. Without an organized system, it becomes difficult to track who asked first, who is currently being helped, and who still needs assistance. ClassAssist solves this problem by organizing requests into a live queue that prioritizes students based on wait time. This creates fairness, consistency, and efficiency during instruction.

---

## Core Functionality

ClassAssist connects directly to a Google Sheet and performs the following functions:

- Polls the sheet on a set interval
- Displays active tickets marked **OPEN** or **IN_PROGRESS**
- Allows the teacher to claim, resolve, reopen, or mark a request as **no show**
- Supports optional passcode protection for write actions
- Suggests the next student to help using a wait time first system

---

## Technology Stack

- Python 3 with Flask
- Google Sheets API (google-api-python-client)
- Plain HTML, CSS, and JavaScript frontend with no build step

---

## Project Structure

- `app.py` — Flask server and API routes  
- `poller.py` — sheet polling, queue logic, and updates  
- `google_sheets.py` — authenticated Google Sheets access  
- `config.py` — configuration and path loader  
- `templates/dashboard.html` — dashboard interface  

---

## Requirements

- Python 3.10 or higher recommended
- A Google Cloud service account with spreadsheet access
- A spreadsheet tab containing required ticket columns

---

## Setup

### 1. Clone repository

```bash
git clone <your-repo-url>
cd ClassAssist
```

### 2. Create environment and install dependencies

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate

# Mac/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Add credentials

Place your service account JSON file here:

```
secrets/service_account.json
```

Make sure the service account email has editor access to the spreadsheet.

### 4. Create config.json

```json
{
  "sheet_id": "YOUR_SPREADSHEET_ID",
  "worksheet_name": "Form Responses 1",
  "poll_seconds": 30,
  "teacher_name": "Teacher Name",
  "teacher_passcode": "optional-passcode",
  "port": 5000
}
```

---

## Expected Spreadsheet Columns

Headers are read regardless of capitalization or spacing.

### Core Fields

- Student
- Period
- Status
- TicketId
- Timestamp
- Help Type
- ClaimedBy
- ClaimedAt

### Action Fields

- LastUpdated
- NoShowAt, NoShowBy
- ResolvedAt, ResolvedBy
- TeacherTags, TagsAt
- FollowUp, FollowUpAt

---

## Running the Application

```bash
python app.py
```

Then open:

```
http://127.0.0.1:5000
```

---

## API Overview

| Endpoint | Method | Description |
|--------|--------|-------------|
| `/api/tickets` | GET | Returns ticket data |
| `/api/action` | POST | Updates ticket state |
| `/api/suggest` | GET | Suggests next student |

Supported actions:

- claim
- reopen
- resolve
- no_show

---

## Authentication

If a teacher passcode or admin token is set in config, write actions require a matching token.

Token may be provided through:

- Header: `X-Auth-Token`
- Query parameter: `token`
- JSON body field: `token`

---

## Troubleshooting

**Missing config.json**
Create the file in the project root.

**Missing service_account.json**
Add it to the secrets folder.

**No tickets appearing**
Verify sheet ID, worksheet name, permissions, and headers.

---

## Notes

- Auto refresh can be paused from the dashboard
- Queue prioritizes longest waiting students
- Only OPEN and IN_PROGRESS tickets display for clarity
