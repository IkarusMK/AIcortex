"""FORWARDED_ALLOW_IPS="*" lets a client forge its IP (uvicorn then takes the FIRST
X-Forwarded-For entry), which defeats the per-IP rate limits. The server keeps
working but warns at startup, and the shipped compose default is not "*"."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_star_trust_warns_at_startup(fresh_server, monkeypatch, capsys):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    cfg = fresh_server.uvicorn_config()
    assert cfg["server_header"] is False
    assert "FORWARDED_ALLOW_IPS" in capsys.readouterr().out


def test_scoped_trust_is_quiet(fresh_server, monkeypatch, capsys):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1,172.16.0.0/12")
    fresh_server.uvicorn_config()
    assert "FORWARDED_ALLOW_IPS" not in capsys.readouterr().out


def test_compose_default_is_not_star():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "FORWARDED_ALLOW_IPS:" in ln)
    assert ":-*}" not in line and "172.16.0.0/12" in line
