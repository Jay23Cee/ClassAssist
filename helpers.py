from datetime import datetime


def normalize(v):
    return str(v or "").strip()


def safe_parse_timestamp(value):
    if not value:
        return None

    s = str(value).strip()
    formats = [
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]

    for f in formats:
        try:
            return datetime.strptime(s, f)
        except Exception:
            pass

    return None


def col_to_letter(n):
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
