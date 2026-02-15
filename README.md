ClassAssist

ClassAssist is a lightweight Flask dashboard designed to manage live student help requests submitted through a Google Form and stored in Google Sheets. It provides teachers with a clear, fair, and structured way to respond to students who need assistance, ensuring support is given in order rather than based on who calls out first.

Purpose

In active classrooms, several students may request help at the same time. Without an organized system, it becomes difficult to track who asked first, who is currently being helped, and who still needs assistance. ClassAssist solves this problem by organizing requests into a live queue that prioritizes students based on wait time. This creates fairness, consistency, and efficiency during instruction.

Core Functionality

ClassAssist connects directly to a Google Sheet and performs the following functions:

Polls the sheet on a set interval
Displays active tickets marked OPEN or IN_PROGRESS
Allows the teacher to claim, resolve, reopen, or mark a request as no show
Supports optional passcode protection for write actions
Suggests the next student to help using a wait time first system

Technology Stack

Python 3 with Flask
Google Sheets API using google api python client
Plain HTML, CSS, and JavaScript frontend with no build step

Project Structure

app.py handles the Flask server and API routes
poller.py manages sheet polling, queue logic, and updates
google_sheets.py manages authenticated Google Sheets access
config.py loads configuration and paths
templates/dashboard.html contains the dashboard interface

Requirements

Python version 3.10 or higher is recommended
A Google Cloud service account with permission to access the spreadsheet
A spreadsheet tab that contains the required ticket columns

Setup

Clone the repository and enter the project folder.
Create a virtual environment and install dependencies from requirements.txt.
Place your Google service account JSON file in the secrets folder under the name service_account.json.
Create a config.json file in the root of the project that includes your spreadsheet ID, worksheet name, polling interval, teacher name, optional passcode, and port number.

Expected Spreadsheet Columns

The application reads and writes spreadsheet headers regardless of capitalization or spacing.

Core read fields include Student, Period, Status, TicketId, Timestamp, Help Type, ClaimedBy, and ClaimedAt.

Update fields used during actions include LastUpdated, NoShowAt, NoShowBy, ResolvedAt, ResolvedBy, TeacherTags, TagsAt, FollowUp, and FollowUpAt.

Running the Application

Start the server by running the main application file. After launch, open the local browser address that points to the configured port to view the dashboard.

API Overview

The system includes endpoints that return ticket data, update ticket states, and suggest the next student to help. Supported actions include claim, reopen, resolve, and no_show.

Authentication Behavior

If a teacher passcode or admin token is configured, write actions require a matching token. Tokens can be provided through request headers, query parameters, or JSON body fields.

Troubleshooting

If config.json is missing, create it in the project root.
If service_account.json is missing, add it to the secrets folder.
If no tickets appear, confirm the spreadsheet ID and worksheet name are correct, verify the service account has permission to access the sheet, and ensure the required headers exist.

Notes

Auto refresh can be paused directly from the dashboard.
The queue prioritizes students who have been waiting the longest.
Only OPEN and IN_PROGRESS tickets are displayed to keep the interface focused and uncluttered.
