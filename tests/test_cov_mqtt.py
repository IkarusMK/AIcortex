"""MQTT tools: registration, vault-resolved credentials, TLS options, SSRF guard.

Proves, with no real broker or DNS (paho's Client, socket.getaddrinfo, the CONNACK
wait and the polling clock are faked; netguard still checks the fake resolution):
registration stores only the secret NAME; the password is read from the vault,
handed to paho and never shows in output or on disk; TLS is verified by default
and relaxed only by the stored options; private targets are refused before a
client is built and the connect-time guard rejects DNS rebinding; publish and get
send/return the right payloads, clean up on every path and cap their output.
"""
import asyncio
import socket
import ssl
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import paho.mqtt.client as paho_client
import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastmcp import Client

PASSWORD = "test-password"
HOST = "broker.example.net"


class _Resolver:
    """Fake socket.getaddrinfo: host -> rounds of IPs (the last round repeats)."""

    def __init__(self):
        self.table = {}

    def set(self, host, *rounds):
        self.table[host] = [[r] if isinstance(r, str) else list(r) for r in rounds]

    def __call__(self, host, *args, **kwargs):
        if host not in self.table:
            raise socket.gaierror(f"fake DNS: no record for {host}")
        rounds = self.table[host]
        ips = rounds.pop(0) if len(rounds) > 1 else rounds[0]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


class _RC:
    def __init__(self, failure, text="Success"):
        self.is_failure, self.text = failure, text

    def __str__(self):
        return self.text


class _Info:
    def __init__(self, ok, waits):
        self.ok, self.waits = ok, waits

    def wait_for_publish(self, timeout=None):
        self.waits.append(timeout)

    def is_published(self):
        return self.ok


class _Broker:
    """A paho Client factory plus the scripted broker behind it."""

    def __init__(self):
        self.clients, self.connack, self.confirmed = [], _RC(False), True  # connack None = silence
        self.connect_error = self.publish_error = self.subscribe_error = None
        self.inbound, self.replies = [], []  # delivered on subscribe / after a publish

    def make(self, api, client_id=None):
        client = _Client(self, api, client_id)
        self.clients.append(client)
        return client


class _Client:
    def __init__(self, broker, api, client_id):
        self.b, self.api, self.client_id = broker, api, client_id
        self.auth = self.ctx = self.connected_to = self.on_message = None
        self.published, self.subscribed, self.waits = [], [], []
        self.stopped = self.disconnected = 0

    def username_pw_set(self, user, password=None):
        self.auth = (user, password)

    def tls_set_context(self, ctx):
        self.ctx = ctx

    def connect(self, host, port, keepalive=60):
        socket.getaddrinfo(host, None)  # connect-time DNS (guarded)
        if self.b.connect_error:
            raise self.b.connect_error
        self.connected_to = (host, port, keepalive)

    def loop_start(self):
        if self.b.connack is not None:
            self.on_connect(self, None, {}, self.b.connack)

    def loop_stop(self):
        self.stopped += 1

    def disconnect(self):
        self.disconnected += 1

    def _deliver(self, items):
        for topic, payload in items:
            self.on_message(self, None, SimpleNamespace(topic=topic, payload=payload))

    def publish(self, topic, payload, qos=0):
        if self.b.publish_error:
            raise self.b.publish_error
        self.published.append((topic, payload, qos))
        self._deliver(self.b.replies)
        return _Info(self.b.confirmed, self.waits)

    def subscribe(self, topic, qos=0):
        if self.b.subscribe_error:
            raise self.b.subscribe_error
        self.subscribed.append((topic, qos))
        self._deliver(self.b.inbound)


class _FastEvent(threading.Event):
    def wait(self, timeout=None):
        return super().wait(min(timeout or 0.05, 0.05))  # never block for the real 10 s


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Env:
    def __init__(self, server, dns, broker, mod, clock):
        self.server, self.dns, self.broker, self.mod, self.clock = server, dns, broker, mod, clock

    @property
    def last(self):
        return self.broker.clients[-1]

    def call(self, tool, **args):
        async def go():
            async with Client(self.server.mcp) as client:
                res = await client.call_tool(tool, args, raise_on_error=False)
                return res.content[0].text if res.content else ""
        return asyncio.run(go())

    def add(self, name="plant", **over):
        args = {"name": name, "host": HOST, "username": "alice", "password_env": "MQ_PW"}
        return self.call("mqtt_add", **{**args, **over})


@pytest.fixture
def env(fresh_server, monkeypatch):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "203.0.113.0/24")
    dns, broker, clock, mod = _Resolver(), _Broker(), _Clock(), sys.modules["mqtt_tools"]
    dns.set(HOST, "203.0.113.30")
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(paho_client, "Client", broker.make)
    monkeypatch.setattr(mod, "threading", SimpleNamespace(Event=_FastEvent, Lock=threading.Lock))
    monkeypatch.setattr(mod, "time", SimpleNamespace(time=clock.time, sleep=clock.sleep))
    # the hosts these tests register — bound by a person, as only a person binds a secret (2.6.0)
    sys.modules["secrets_store"].vault_set("MQ_PW", PASSWORD, hosts=[HOST, "lan.example.net", "odd.example.net", "rebind.example.net"])
    return _Env(fresh_server, dns, broker, mod, clock)


@pytest.fixture
def plant(env):
    env.add()
    return env


def _write_ca(path):
    key, now = ec.generate_private_key(ec.SECP256R1()), datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca.example.com")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(path)


PUBLISH, GET = ("mqtt_publish", {"topic": "t", "payload_str": "x"}), ("mqtt_get", {"topic": "t"})


# --- registration ---------------------------------------------------------
def test_add_normalises_slug_and_stores_only_the_secret_name(env):
    out = env.add("Plant Floor 1!", ca_bundle="  /certs/lan-ca.pem ")
    assert out == "Registered MQTT broker 'plant-floor-1'."  # secret exists: no hint
    assert env.add("!!!", password_env="UNSET_PW").startswith("Registered MQTT broker 'broker'.")
    assert "secret_set('UNSET_PW', <value>, hosts='broker.example.net')" in env.add(password_env="UNSET_PW")  # secret missing: store it with its host
    cfg = (env.mod.MQTT_DIR / "plant-floor-1.json").read_text()
    assert '"password_env": "MQ_PW"' in cfg and '"ca_bundle": "/certs/lan-ca.pem"' in cfg
    assert '"port": 8883' in cfg and '"tls": true' in cfg and PASSWORD not in cfg


def test_add_reports_an_unusable_store_instead_of_raising(env, tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(env.mod, "MQTT_DIR", blocker)
    assert env.add().startswith("Could not register broker:")


def test_list_and_delete_brokers_incl_unreadable_config(env):
    assert env.call("mqtt_list") == "No MQTT brokers configured yet."  # no directory yet
    env.add(description="mine")
    (env.mod.MQTT_DIR / "broken.json").write_text("{not json")
    assert env.call("mqtt_list").splitlines() == ["- broken — (unreadable config)", f"- plant — {HOST}:8883 — mine"]
    assert env.call("mqtt_get", broker="broken", topic="t").startswith("Unknown broker 'broken'")
    assert env.call("mqtt_delete", name="Plant") == "Deleted MQTT broker 'plant'."
    assert env.call("mqtt_delete", name="plant") == "No broker 'plant'."
    env.call("mqtt_delete", name="broken")
    assert env.call("mqtt_list") == "No MQTT brokers configured yet. Use mqtt_add."
    assert env.broker.clients == []


@pytest.mark.parametrize("tool,extra", [PUBLISH, GET])
def test_unknown_broker_is_refused_without_connecting(env, tool, extra):
    assert env.call(tool, broker="nope", **extra) == "Unknown broker 'nope'. Use mqtt_list / mqtt_add."
    assert env.broker.clients == []


# --- credentials, TLS and ports ---------------------------------------------
def test_password_comes_from_the_vault_and_is_never_echoed(plant):
    outputs = [plant.call("mqtt_publish", broker="plant", topic="t", payload_str="x"),
               plant.call("mqtt_get", broker="plant", topic="t", timeout_s=1), plant.call("mqtt_list")]
    assert plant.last.auth == ("alice", PASSWORD) and PASSWORD not in "".join(outputs)
    assert (plant.last.connected_to, plant.last.api) == ((HOST, 8883, 30), paho_client.CallbackAPIVersion.VERSION2)


def test_username_without_secret_name_connects_with_no_password_and_anonymous_sets_no_auth(env):
    env.add(password_env="")
    env.call("mqtt_publish", broker="plant", **PUBLISH[1])
    assert env.last.auth == ("alice", None)
    env.add(username="", password_env="")
    env.call("mqtt_publish", broker="plant", **PUBLISH[1])
    assert env.last.auth is None


def test_client_id_is_generated_unless_configured(env):
    env.add()
    env.call("mqtt_publish", broker="plant", **PUBLISH[1])
    assert env.last.client_id.startswith("llmconn-") and len(env.last.client_id) == len("llmconn-") + 8
    env.add(client_id="cortex-test")
    env.call("mqtt_publish", broker="plant", **PUBLISH[1])
    assert env.last.client_id == "cortex-test"


@pytest.mark.parametrize("over,port", [({"port": 0, "tls": False}, 1883), ({"port": 0}, 8883),
                                       ({"port": 1884, "tls": False}, 1884)])
def test_port_defaults_follow_the_tls_setting(env, over, port):
    env.add(**over)
    env.call("mqtt_publish", broker="plant", **PUBLISH[1])
    assert env.last.connected_to[1] == port and (env.last.ctx is not None) == over.get("tls", True)


@pytest.mark.parametrize("over,mode,hostname_check", [({}, ssl.CERT_REQUIRED, True),
                                                      ({"tls_insecure": True}, ssl.CERT_NONE, False)])
def test_tls_is_verified_by_default_and_relaxed_only_by_the_stored_option(env, over, mode, hostname_check):
    env.add(**over)
    env.call("mqtt_publish", broker="plant", **PUBLISH[1])
    assert (env.last.ctx.verify_mode, env.last.ctx.check_hostname) == (mode, hostname_check)


def test_ca_bundle_is_loaded_into_the_tls_context_and_a_bad_path_fails_closed(env, tmp_path):
    env.add(ca_bundle=_write_ca(tmp_path / "ca.pem"), tls_insecure=True)  # the CA wins over tls_insecure
    env.call("mqtt_publish", broker="plant", **PUBLISH[1])
    baseline = ssl.create_default_context().cert_store_stats()["x509"]
    assert env.last.ctx.verify_mode == ssl.CERT_REQUIRED and env.last.ctx.cert_store_stats()["x509"] == baseline + 1
    env.add(ca_bundle=str(tmp_path / "missing.pem"))
    out = env.call("mqtt_publish", broker="plant", **PUBLISH[1])
    assert out.startswith("Connect failed:") and env.last.connected_to is None


# --- SSRF guard and connect failures ------------------------------------------
@pytest.mark.parametrize("tool,extra", [PUBLISH, GET])
def test_private_target_is_blocked_before_a_client_is_built(env, tool, extra):
    env.dns.set("lan.example.net", "10.0.0.5")
    env.add(host="lan.example.net")
    out = env.call(tool, broker="plant", **extra)
    assert out.startswith("Connect failed: Blocked by network policy — blocked: 10.0.0.5")
    assert env.broker.clients == []


@pytest.mark.parametrize("ips,fragment", [("169.254.169.254", "169.254.169.254"), (None, "cannot resolve host")])
def test_metadata_and_unresolvable_hosts_stay_blocked(env, monkeypatch, ips, fragment):
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "10.0.0.0/8")  # a LAN allow-list must not open these
    ips and env.dns.set("odd.example.net", ips)
    env.add(host="odd.example.net")
    out = env.call("mqtt_publish", broker="plant", **PUBLISH[1])
    assert out.startswith("Connect failed: Blocked by network policy") and fragment in out


def test_connect_time_guard_rejects_a_dns_rebinding_answer(env):
    env.dns.set("rebind.example.net", "203.0.113.20", "10.0.0.9")  # check, then connect
    env.add(host="rebind.example.net")
    out = env.call("mqtt_publish", broker="plant", **PUBLISH[1])
    assert out.startswith("Connect failed:") and "internal/blocked at connect" in out
    assert env.last.connected_to is None and env.last.published == []


@pytest.mark.parametrize("setup,expected", [
    (lambda b: setattr(b, "connack", _RC(True, "Not authorized")), "Connect failed: MQTT connect refused: Not authorized"),
    (lambda b: setattr(b, "connack", None), "Connect failed: MQTT connect timed out (no CONNACK)"),
    (lambda b: setattr(b, "connect_error", OSError("network unreachable")), "Connect failed: network unreachable")])
@pytest.mark.parametrize("tool,extra", [PUBLISH, GET])
def test_connect_failures_are_reported_and_nothing_is_sent(plant, setup, expected, tool, extra):
    setup(plant.broker)
    assert plant.call(tool, broker="plant", **extra) == expected
    assert plant.last.published == [] and plant.last.subscribed == []


# --- publish -----------------------------------------------------------------
@pytest.mark.parametrize("args,body", [({"payload": {"cmd": "on", "level": 3}}, '{"cmd": "on", "level": 3}'),
                                       ({"payload": {}}, "{}"), ({"payload_str": "raw text"}, "raw text"),
                                       ({"payload": {"a": 1}, "payload_str": "ignored"}, '{"a": 1}'), ({}, "")])
def test_publish_sends_json_or_raw_payload_and_cleans_up(plant, args, body):
    out = plant.call("mqtt_publish", broker="plant", topic="plant/light/set", qos=1, **args)
    assert out == "Published to 'plant/light/set' (qos=1): ok"
    assert plant.last.published == [("plant/light/set", body, 1)] and plant.last.waits == [10]
    assert (plant.last.stopped, plant.last.disconnected) == (1, 1)


def test_publish_reports_unconfirmed_and_failed_publishes_and_still_disconnects(plant):
    plant.broker.confirmed = False
    assert plant.call("mqtt_publish", broker="plant", topic="t").endswith("NOT confirmed")
    plant.broker.publish_error = RuntimeError("queue full")
    assert plant.call("mqtt_publish", broker="plant", topic="t") == "Publish failed: queue full"
    assert (plant.last.stopped, plant.last.disconnected) == (1, 1)


# --- get -----------------------------------------------------------------------
def test_get_returns_formatted_messages_up_to_the_limit_and_cleans_up(plant):
    plant.broker.inbound = [("plant/temp", b"21.5"), ("plant/hum", b"40"), ("plant/co2", b"700")]
    out = plant.call("mqtt_get", broker="plant", topic="plant/#", max_messages=2)
    assert out == "[plant/temp] 21.5\n[plant/hum] 40"
    assert plant.last.subscribed == [("plant/#", 0)] and (plant.last.stopped, plant.last.disconnected) == (1, 1)
    assert plant.call("mqtt_get", broker="plant", topic="plant/#", max_messages=0) == "[plant/temp] 21.5"  # min 1


def test_get_triggers_a_report_with_the_request_payload_when_both_parts_are_given(plant):
    plant.broker.replies = [("plant/report", b'{"state": "idle"}')]
    out = plant.call("mqtt_get", broker="plant", topic="plant/report", request_topic="plant/request",
                     request_payload={"cmd": "pushall"})
    assert out == '[plant/report] {"state": "idle"}'
    assert plant.last.published == [("plant/request", '{"cmd": "pushall"}', 0)]
    plant.call("mqtt_get", broker="plant", topic="plant/report", timeout_s=1, request_topic="plant/request")
    assert plant.last.published == []  # a topic without a payload sends nothing


@pytest.mark.parametrize("timeout_s,waited", [(3, 3), (0, 1)])
def test_get_waits_for_the_timeout_then_says_nothing_arrived(plant, timeout_s, waited):
    start = plant.clock.now
    out = plant.call("mqtt_get", broker="plant", topic="plant/quiet", timeout_s=timeout_s)
    assert out == f"No message on 'plant/quiet' within {timeout_s}s."
    assert waited <= plant.clock.now - start < waited + 0.2


def test_get_decodes_leniently_and_caps_the_output(plant):
    plant.broker.inbound = [("t", b"caf\xe9"), ("t", "plain str"), ("t", b"x" * 7000)]
    lines = plant.call("mqtt_get", broker="plant", topic="t", max_messages=3).split("\n")
    assert lines[0] == "[t] caf�" and lines[1] == "[t] 'plain str'"  # bad bytes replaced; no .decode -> repr
    assert len("\n".join(lines)) <= 6000 + len("\n…(truncated)") and lines[-1] == "…(truncated)"


def test_get_reports_a_failed_subscribe_and_still_disconnects(plant):
    plant.broker.subscribe_error = RuntimeError("not allowed")
    assert plant.call("mqtt_get", broker="plant", topic="t") == "Subscribe failed: not allowed"
    assert (plant.last.stopped, plant.last.disconnected) == (1, 1)
