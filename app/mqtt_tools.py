"""Generic, allow-listed MQTT dispatcher — devices/integrations as DATA.

Like ``services.py`` is for HTTP, this lets any MQTT device or broker be added
at RUNTIME as a *broker config* (data) plus a secret — no new code, no redeploy.
``mqtt_publish`` sends a command;
``mqtt_get`` subscribes briefly and returns what the device reports.

Broker configs live under MQTT_DIR. The password is referenced by NAME
(``password_env``) and resolved server-side via ``secrets_store`` — never stored
in data, never returned to the model. Only registered brokers can be reached.
"""
import json
import os
import re
import ssl
import threading
import time
import uuid
from pathlib import Path

import netguard
import secrets_store

MQTT_DIR = Path(os.environ.get("MQTT_DIR", "/data/mqtt"))


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "broker"


def _cfg_path(name: str) -> Path:
    return MQTT_DIR / f"{_slug(name)}.json"


def _load(name: str):
    p = _cfg_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _new_client(cfg):
    """Build a connected paho client with its network loop started.

    Raises on connection/auth failure or timeout. Caller must loop_stop() +
    disconnect() when done (use try/finally).
    """
    ok, reason = netguard.check_host(cfg.get("host", ""))
    if not ok:
        raise ConnectionError(f"Blocked by network policy — {reason}")

    import paho.mqtt.client as mqtt

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=cfg.get("client_id") or f"llmconn-{uuid.uuid4().hex[:8]}",
    )
    username = cfg.get("username")
    if username:
        password = None
        if cfg.get("password_env"):
            password = secrets_store.get_secret(cfg["password_env"])
        client.username_pw_set(username, password)

    if cfg.get("tls"):
        ctx = ssl.create_default_context()
        if cfg.get("tls_insecure"):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        client.tls_set_context(ctx)

    state = {"rc": None}
    connected = threading.Event()

    def on_connect(c, u, flags, reason_code, properties=None):
        state["rc"] = reason_code
        connected.set()

    client.on_connect = on_connect
    port = int(cfg.get("port") or (8883 if cfg.get("tls") else 1883))
    # guard(host): enforce the egress IP policy at CONNECT time (anti DNS-rebinding)
    with netguard.guard(cfg.get("host", "")):
        client.connect(cfg["host"], port, keepalive=30)
    client.loop_start()
    if not connected.wait(timeout=10):
        client.loop_stop()
        raise TimeoutError("MQTT connect timed out (no CONNACK)")
    rc = state["rc"]
    if rc is not None and getattr(rc, "is_failure", False):
        client.loop_stop()
        raise ConnectionError(f"MQTT connect refused: {rc}")
    return client


def register(mcp):
    @mcp.tool
    def mqtt_add(name: str, host: str, port: int = 8883, tls: bool = True,
                 tls_insecure: bool = False, username: str = "",
                 password_env: str = "", client_id: str = "",
                 description: str = "") -> str:
        """Register/update an MQTT broker or device as DATA (no redeploy).

        password_env = NAME of the secret holding the password (store it with
        secret_set); never stored here. TLS certificates are VERIFIED by default;
        only set tls_insecure=true for a self-signed LAN device. Example for such a
        device: host=<device-ip>, port=8883, tls=true, tls_insecure=true,
        username=<user>, password_env=<secret-name>."""
        try:
            MQTT_DIR.mkdir(parents=True, exist_ok=True)
            cfg = {
                "name": name,
                "host": host,
                "port": int(port),
                "tls": bool(tls),
                "tls_insecure": bool(tls_insecure),
                "username": username,
                "password_env": password_env,
                "client_id": client_id,
                "description": description,
            }
            _cfg_path(name).write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            note = ""
            if password_env and not secrets_store.get_secret(password_env):
                note = f" — set the password with secret_set('{password_env}', <value>)"
            return f"Registered MQTT broker '{_slug(name)}'.{note}"
        except Exception as exc:
            return f"Could not register broker: {exc}"

    @mcp.tool
    def mqtt_list() -> str:
        """List configured MQTT brokers (name — host:port — description)."""
        if not MQTT_DIR.exists():
            return "No MQTT brokers configured yet."
        items = sorted(MQTT_DIR.glob("*.json"))
        if not items:
            return "No MQTT brokers configured yet. Use mqtt_add."
        out = []
        for p in items:
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                out.append(f"- {p.stem} — {c.get('host', '')}:{c.get('port', '')} — {c.get('description', '')}")
            except Exception:
                out.append(f"- {p.stem} — (unreadable config)")
        return "\n".join(out)

    @mcp.tool
    def mqtt_delete(name: str) -> str:
        """Remove a registered MQTT broker by name."""
        p = _cfg_path(name)
        if p.exists():
            p.unlink()
            return f"Deleted MQTT broker '{_slug(name)}'."
        return f"No broker '{name}'."

    @mcp.tool
    def mqtt_publish(broker: str, topic: str, payload: dict = None,
                     payload_str: str = "", qos: int = 0) -> str:
        """Publish to a topic on a registered broker. Pass JSON via `payload`
        (a dict) OR a raw string via `payload_str`. STATE-CHANGING for device
        command topics — confirm with the user before sending commands."""
        cfg = _load(broker)
        if not cfg:
            return f"Unknown broker '{broker}'. Use mqtt_list / mqtt_add."
        body = json.dumps(payload) if payload is not None else (payload_str or "")
        try:
            client = _new_client(cfg)
        except Exception as exc:
            return f"Connect failed: {exc}"
        try:
            info = client.publish(topic, body, qos=int(qos))
            info.wait_for_publish(timeout=10)
            return f"Published to '{topic}' (qos={qos}): {'ok' if info.is_published() else 'NOT confirmed'}"
        except Exception as exc:
            return f"Publish failed: {exc}"
        finally:
            client.loop_stop()
            client.disconnect()

    @mcp.tool
    def mqtt_get(broker: str, topic: str, timeout_s: int = 5,
                 max_messages: int = 1, request_topic: str = "",
                 request_payload: dict = None) -> str:
        """Subscribe to `topic` and return up to `max_messages` messages received
        within `timeout_s`. Optionally first publish `request_payload` (dict) to
        `request_topic` to trigger a fresh report from devices that report on
        demand (a common request/report topic pattern)."""
        cfg = _load(broker)
        if not cfg:
            return f"Unknown broker '{broker}'. Use mqtt_list / mqtt_add."
        received = []
        lock = threading.Lock()

        def on_message(c, u, msg):
            try:
                text = msg.payload.decode("utf-8", "replace")
            except Exception:
                text = repr(msg.payload)
            with lock:
                received.append(f"[{msg.topic}] {text}")

        try:
            client = _new_client(cfg)
        except Exception as exc:
            return f"Connect failed: {exc}"
        try:
            client.on_message = on_message
            client.subscribe(topic, qos=0)
            if request_topic and request_payload is not None:
                client.publish(request_topic, json.dumps(request_payload), qos=0)
            limit = max(1, int(max_messages))
            deadline = time.time() + max(1, int(timeout_s))
            while time.time() < deadline:
                with lock:
                    if len(received) >= limit:
                        break
                time.sleep(0.1)
            with lock:
                msgs = received[:limit]
            if not msgs:
                return f"No message on '{topic}' within {timeout_s}s."
            out = "\n".join(msgs)
            if len(out) > 6000:
                out = out[:6000] + "\n…(truncated)"
            return out
        except Exception as exc:
            return f"Subscribe failed: {exc}"
        finally:
            client.loop_stop()
            client.disconnect()
