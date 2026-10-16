from config.proxy import normalize_proxy_url


def test_normalize_proxy_accepts_provider_four_field_format():
    assert normalize_proxy_url("proxy.example:5782:user:pass") == "http://user:pass@proxy.example:5782"


def test_normalize_proxy_preserves_url_format():
    assert normalize_proxy_url("socks5h://user:pass@proxy.example:5782") == "socks5h://user:pass@proxy.example:5782"


def test_normalize_proxy_errors_keep_complete_input_for_local_audit():
    value = "proxy.example:not-a-port:secret-user:secret-password"
    try:
        normalize_proxy_url(value)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected invalid proxy to raise")
    assert "secret-user" in message
    assert "secret-password" in message
