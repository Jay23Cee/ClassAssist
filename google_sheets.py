import os

from google.oauth2 import service_account
from googleapiclient.discovery import build

from config import SECRETS_PATH


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def build_sheets_service():
    if not os.path.exists(SECRETS_PATH):
        raise FileNotFoundError("Missing service_account.json")

    creds = service_account.Credentials.from_service_account_file(
        SECRETS_PATH,
        scopes=SCOPES,
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)
