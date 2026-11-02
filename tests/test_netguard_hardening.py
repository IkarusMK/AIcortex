"""TLS verification is only switched off by an explicit yes.

`tls_insecure` was read with bool(), so a hand-edited config holding the text
"false" (or "0", "no") turned certificate checks OFF. The *_add tools store a real
boolean, so this only bit hand-written configs — now any text but a clear yes keeps
verification on.
"""
import ssl
import sys

import pytest


@pytest.fixture
def ng(fresh_server):
    return sys.modules["netguard"]


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off", "", None, False, 0])
def test_anything_but_a_clear_yes_keeps_verification_on(ng, value):
    cfg = {"tls_insecure": value}
    assert ng.tls_verify(cfg) is True
    assert ng.ssl_context(cfg).verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("value", [True, 1, "true", "TRUE", "1", "yes", "on"])
def test_a_clear_yes_turns_verification_off(ng, value):
    cfg = {"tls_insecure": value}
    assert ng.tls_verify(cfg) is False
    assert ng.ssl_context(cfg).verify_mode == ssl.CERT_NONE
