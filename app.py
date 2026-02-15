from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from auth import get_expected_token, require_write_auth
from config import load_config
from helpers import normalize
from logging_setup import get_logger
from poller import SheetPoller


logger = get_logger()


def create_app(cfg=None):
    app = Flask(__name__, template_folder="templates")
    app.config["JSONIFY_PRETTYPRINT_REGULAR"] = False

    cfg = cfg or load_config()
    poller = SheetPoller(cfg)
    poller.start()

    app.config["CFG"] = cfg
    app.config["POLLER"] = poller

    @app.after_request
    def no_cache(resp):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

    @app.errorhandler(401)
    def err_401(_e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "message": "UNAUTHORIZED"}), 401
        return "UNAUTHORIZED", 401

    @app.errorhandler(404)
    def err_404(_e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "message": "NOT_FOUND"}), 404
        return "NOT_FOUND", 404

    @app.errorhandler(500)
    def err_500(_e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "message": "SERVER_ERROR"}), 500
        return "SERVER_ERROR", 500

    @app.route("/")
    def dashboard():
        return render_template("dashboard.html")

    @app.route("/api/tickets")
    def api_tickets():
        tickets, meta = app.config["POLLER"].get_state()
        cfg_local = app.config["CFG"]
        return jsonify(
            {
                "tickets": tickets,
                "meta": meta,
                "poll_seconds": int(cfg_local.get("poll_seconds", 30)),
                "teacher_name": normalize(cfg_local.get("teacher_name", "Teacher")),
                "auth_enabled": bool(get_expected_token(cfg_local)),
            }
        )

    @app.route("/api/action", methods=["POST"])
    def api_action():
        try:
            cfg_local = app.config["CFG"]
            require_write_auth(cfg_local)
            data = request.get_json(force=True, silent=True) or {}
            ticket_id = data.get("ticket_id", "")
            action = data.get("action", "")
            tags = data.get("tags", "")
            ok, msg = app.config["POLLER"].apply_action(ticket_id, action, tags=tags)
            return jsonify({"ok": ok, "message": msg})
        except HTTPException:
            raise
        except Exception:
            logger.exception("api_action error")
            return jsonify({"ok": False, "message": "SERVER_ERROR"}), 500

    @app.route("/api/suggest")
    def api_suggest():
        period = request.args.get("period", "")
        help_type = request.args.get("help_type", "")
        status = request.args.get("status", "OPEN")
        ticket = app.config["POLLER"].suggest_next(
            period_filter=period,
            help_type_filter=help_type,
            status_filter=status,
        )
        return jsonify({"ticket": ticket})

    return app


def main():
    cfg = load_config()
    host = "127.0.0.1"
    port = int(cfg.get("port", 5000))

    app = create_app(cfg)
    print(f"\nRunning → http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
