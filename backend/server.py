from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import unquote, urlparse


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import FRONTEND_DIR, env_bool, env_int, load_dotenv
from backend.lamp_runtime import LampRuntime
from backend.security import AuthManager
from backend.storage import PresetStore, ValidationError


class HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class AppContext:
    store: PresetStore
    auth: AuthManager
    lamps: LampRuntime


class MusicLightServer(ThreadingHTTPServer):
    def __init__(self, address, context: AppContext):
        super().__init__(address, MusicLightHandler)
        self.context = context


class MusicLightHandler(BaseHTTPRequestHandler):
    server: MusicLightServer

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _dispatch(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/"):
                self._handle_api(path)
            else:
                self._serve_static(path)
        except HttpError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except ValidationError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Некорректный JSON"})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_api(self, path: str):
        method = self.command.upper()
        parts = [part for part in path.strip("/").split("/") if part]

        if method == "GET" and path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "lamp_runtime": self.server.context.lamps.status()})
            return

        if method == "POST" and path == "/api/login":
            self._login()
            return

        if method == "POST" and path == "/api/logout":
            token = self._bearer_token()
            if token:
                self.server.context.auth.logout(token)
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        role = self._require_auth()

        if method == "GET" and path == "/api/state":
            self._send_json(HTTPStatus.OK, self._state_payload(role))
            return

        if method == "POST" and path == "/api/apply":
            self._apply_preset(role)
            return

        if method == "POST" and path == "/api/diagnostics":
            self._require_admin(role)
            result = run_diagnostics(self.server.context)
            payload = self._state_payload(role)
            payload["diagnostics_result"] = result
            self._send_json(HTTPStatus.OK, payload)
            return

        if method == "POST" and path == "/api/presets":
            self._require_admin(role)
            preset = self.server.context.store.create_preset(self._read_json_body())
            self._send_json(HTTPStatus.CREATED, {"preset": preset, "state": self._state_payload(role)})
            return

        if len(parts) == 3 and parts[:2] == ["api", "presets"]:
            preset_id = parts[2]
            if method == "PUT":
                self._require_admin(role)
                preset = self.server.context.store.update_preset(preset_id, self._read_json_body())
                if not preset:
                    raise HttpError(HTTPStatus.NOT_FOUND, "Пресет не найден")
                self._send_json(HTTPStatus.OK, {"preset": preset, "state": self._state_payload(role)})
                return
            if method == "DELETE":
                self._require_admin(role)
                was_current = self.server.context.store.get_runtime_state().get("current_preset_id") == preset_id
                deleted = self.server.context.store.delete_preset(preset_id)
                if not deleted:
                    raise HttpError(HTTPStatus.NOT_FOUND, "Пресет не найден")
                if was_current:
                    self.server.context.lamps.stop_animation()
                self._send_json(HTTPStatus.OK, {"ok": True, "state": self._state_payload(role)})
                return

        raise HttpError(HTTPStatus.NOT_FOUND, "Маршрут не найден")

    def _login(self):
        body = self._read_json_body()
        role = str(body.get("role") or "").strip().lower()
        password = str(body.get("password") or "")
        token = self.server.context.auth.login(role, password)
        if not token:
            raise HttpError(HTTPStatus.UNAUTHORIZED, "Неверный пароль")

        self._send_json(
            HTTPStatus.OK,
            {
                "token": token,
                "role": role,
                "state": self._state_payload(role),
            },
        )

    def _apply_preset(self, role: str):
        body = self._read_json_body()
        preset_id = str(body.get("preset_id") or "").strip()
        preset = self.server.context.store.get_preset(preset_id)
        if not preset:
            raise HttpError(HTTPStatus.NOT_FOUND, "Пресет не найден")

        lamps = self.server.context.store.list_lamps()
        result = self.server.context.lamps.apply_preset(preset, lamps)
        if result.get("ok"):
            self.server.context.store.set_current_preset(preset_id, result)
            status = HTTPStatus.OK
        else:
            status = HTTPStatus.BAD_GATEWAY

        payload = self._state_payload(role)
        payload["apply_result"] = result
        self._send_json(status, payload)

    def _state_payload(self, role: str) -> dict:
        store = self.server.context.store
        return {
            "role": role,
            "lamps": store.list_lamps(),
            "presets": store.list_presets(),
            "runtime": store.get_runtime_state(),
            "lamp_runtime": self.server.context.lamps.status(),
        }

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 1_000_000:
            raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Слишком большой запрос")
        if length == 0:
            return {}
        body = self.rfile.read(length).decode("utf-8")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise HttpError(HTTPStatus.BAD_REQUEST, "Ожидался JSON-объект")
        return data

    def _bearer_token(self) -> str:
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return ""

    def _require_auth(self) -> str:
        token = self._bearer_token()
        role = self.server.context.auth.role_for_token(token) if token else None
        if not role:
            raise HttpError(HTTPStatus.UNAUTHORIZED, "Нужна авторизация")
        return role

    def _require_admin(self, role: str) -> None:
        if role != "admin":
            raise HttpError(HTTPStatus.FORBIDDEN, "Нужен пароль администратора")

    def _send_json(self, status: HTTPStatus, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str):
        if path in {"", "/"}:
            target = FRONTEND_DIR / "index.html"
        else:
            relative = unquote(path).lstrip("/")
            target = (FRONTEND_DIR / relative).resolve()
            frontend_root = FRONTEND_DIR.resolve()
            if target != frontend_root and frontend_root not in target.parents:
                raise HttpError(HTTPStatus.FORBIDDEN, "Недоступный путь")
            if not target.exists() or target.is_dir():
                target = FRONTEND_DIR / "index.html"

        if not target.exists():
            raise HttpError(HTTPStatus.NOT_FOUND, "Frontend не найден")

        content = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".js":
            content_type = "application/javascript"
        if target.suffix == ".css":
            content_type = "text/css"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Music Light web service")
    parser.add_argument("--host", default=os.environ.get("MUSICLIGHT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=env_int("MUSICLIGHT_PORT", 8000))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Do not send commands to physical lamps.")
    mode.add_argument("--live", action="store_true", help="Send commands to physical lamps.")
    return parser.parse_args(argv)


def run_diagnostics(context: AppContext) -> dict:
    context.store.set_diagnostics_running(True)
    try:
        result = context.lamps.diagnose(context.store.list_lamps())
    except Exception as exc:
        result = {
            "ok": False,
            "dry_run": context.lamps.dry_run,
            "lamp_count": 0,
            "ok_count": 0,
            "failed_count": 0,
            "rows": [],
            "error": str(exc),
        }
    context.store.set_diagnostics(result)
    return result


def start_initial_diagnostics(context: AppContext) -> None:
    worker = Thread(target=run_diagnostics, args=(context,), daemon=True)
    worker.start()


def main(argv=None):
    load_dotenv()
    args = parse_args(argv)
    dry_run = env_bool("MUSICLIGHT_DRY_RUN", False)
    if args.dry_run:
        dry_run = True
    if args.live:
        dry_run = False

    context = AppContext(
        store=PresetStore(),
        auth=AuthManager(),
        lamps=LampRuntime(dry_run=dry_run),
    )
    start_initial_diagnostics(context)
    server = MusicLightServer((args.host, args.port), context)
    mode = "dry-run" if dry_run else "live"
    print(f"Music Light web service: http://{args.host}:{args.port} ({mode})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
