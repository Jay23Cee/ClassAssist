from flask import abort, request

from helpers import normalize


def get_expected_token(cfg):
    return normalize(cfg.get("teacher_passcode") or cfg.get("admin_token"))


def require_write_auth(cfg):
    expected = get_expected_token(cfg or {})
    if not expected:
        return

    provided = normalize(
        request.headers.get("X-Auth-Token")
        or request.args.get("token")
        or (request.get_json(silent=True) or {}).get("token")
    )

    if provided != expected:
        abort(401, description="UNAUTHORIZED")
