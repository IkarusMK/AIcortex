"""Minimal OIDC provider for the upgrade check: discovery, JWKS, authorize (auto-
approves), token (code + refresh), userinfo, plus /echo that records the headers
it receives (to prove the vault secret is injected by call_service)."""
import base64
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class FakeIdP:
    def __init__(self, sub="alice-sub", email="alice@example.com"):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.sub, self.email = sub, email
        self.nonces = {}
        self.echo_seen = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def jwt(self, claims: dict) -> str:
        head = b64u(json.dumps({"alg": "RS256", "typ": "JWT", "kid": "k1"}).encode())
        body = b64u(json.dumps(claims).encode())
        sig = self.key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{head}.{body}.{b64u(sig)}"

    def jwks(self) -> dict:
        nums = self.key.public_key().public_numbers()

        def raw(x):
            return x.to_bytes((x.bit_length() + 7) // 8, "big")
        return {"keys": [{"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256",
                          "n": b64u(raw(nums.n)), "e": b64u(raw(nums.e))}]}

    def tokens(self, client_id: str, nonce: str = "") -> dict:
        now = int(time.time())
        claims = {"iss": self.base, "sub": self.sub, "aud": client_id, "email": self.email,
                  "iat": now, "exp": now + 3600}
        if nonce:
            claims["nonce"] = nonce
        return {"access_token": self.jwt(claims), "id_token": self.jwt(claims),
                "refresh_token": "rt-" + uuid.uuid4().hex, "token_type": "Bearer",
                "expires_in": 3600, "scope": "openid profile email"}

    def _handler(self):
        idp = self

        class H(BaseHTTPRequestHandler):
            def _json(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path == "/.well-known/openid-configuration":
                    return self._json({
                        "issuer": idp.base,
                        "authorization_endpoint": f"{idp.base}/authorize",
                        "token_endpoint": f"{idp.base}/token",
                        "userinfo_endpoint": f"{idp.base}/userinfo",
                        "jwks_uri": f"{idp.base}/jwks",
                        "response_types_supported": ["code"],
                        "subject_types_supported": ["public"],
                        "id_token_signing_alg_values_supported": ["RS256"],
                        "scopes_supported": ["openid", "profile", "email"],
                        "token_endpoint_auth_methods_supported": ["client_secret_post",
                                                                  "client_secret_basic"],
                        "code_challenge_methods_supported": ["S256"],
                    })
                if url.path == "/jwks":
                    return self._json(idp.jwks())
                if url.path == "/userinfo":
                    return self._json({"sub": idp.sub, "email": idp.email})
                if url.path == "/authorize":
                    code = "c-" + uuid.uuid4().hex
                    idp.nonces[code] = q.get("nonce", "")
                    loc = q["redirect_uri"] + "?" + urlencode({"code": code, "state": q.get("state", "")})
                    self.send_response(302)
                    self.send_header("Location", loc)
                    self.end_headers()
                    return
                if url.path.startswith("/echo"):
                    idp.echo_seen.append(dict(self.headers))
                    return self._json({"ok": True})
                self._json({"error": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                if urlparse(self.path).path == "/token":
                    client_id = form.get("client_id", "aicortex")
                    nonce = idp.nonces.pop(form.get("code", ""), "")
                    return self._json(idp.tokens(client_id, nonce))
                self._json({"error": "not found"}, 404)

            def log_message(self, *args):
                pass

        return H
