import json
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
SECRETS_PATH = os.path.join(APP_DIR, "secrets", "service_account.json")
LOG_DIR = os.path.join(APP_DIR, "logs")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError("Missing config.json")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)
