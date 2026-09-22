from media_tools.core.redact import redact, redact_text, redact_url


def test_redact_url_strips_query_and_fragment():
    assert redact_url("https://host/v/x.m3u8?token=SECRET#frag") == "https://host/v/x.m3u8"


def test_redact_url_strips_userinfo_with_password():
    # I1: the password must not survive redaction just because the query string does.
    out = redact_url("https://alice:hunter2@cdn.example.com/v/x.m3u8?token=SECRET")
    assert out == "https://cdn.example.com/v/x.m3u8"
    assert "alice" not in out
    assert "hunter2" not in out


def test_redact_url_strips_username_only_userinfo():
    out = redact_url("https://alice@cdn.example.com/v/x.m3u8?token=SECRET")
    assert out == "https://cdn.example.com/v/x.m3u8"
    assert "alice" not in out


def test_redact_url_keeps_port_but_drops_credentials():
    out = redact_url("https://alice:hunter2@cdn.example.com:8443/v/x.m3u8?token=SECRET")
    assert out == "https://cdn.example.com:8443/v/x.m3u8"
    assert "alice" not in out
    assert "hunter2" not in out


def test_redact_url_leaves_a_plain_url_unchanged_in_shape():
    assert redact_url("https://cdn.example.com/v/x.m3u8") == "https://cdn.example.com/v/x.m3u8"


def test_redact_url_keeps_ipv6_brackets_with_credentials_and_port():
    # Residual of I1: rebuilding netloc from `.hostname` strips the brackets an IPv6
    # literal needs to tell its own colons apart from a trailing ":port" — without them
    # "::1" (host) + "8443" (port) reads back as a single, wrong host "::1:8443".
    out = redact_url("https://alice:pw@[::1]:8443/path?token=x")
    assert out == "https://[::1]:8443/path"
    assert "alice" not in out
    assert "pw" not in out


def test_redact_url_keeps_ipv6_brackets_without_credentials_or_port():
    out = redact_url("https://[2001:db8::1]/path?token=x")
    assert out == "https://[2001:db8::1]/path"


def test_redact_url_plain_hostname_still_unbracketed():
    # Confirms the IPv6 fix does not affect an ordinary hostname.
    out = redact_url("https://alice:pw@cdn.example.com:8443/path?token=x")
    assert out == "https://cdn.example.com:8443/path"


def test_redact_text_strips_credentials_from_an_embedded_url():
    text = "download failed: https://alice:hunter2@host/x.m3u8?sjwt=SECRET see logs"
    out = redact_text(text)
    assert "hunter2" not in out
    assert "SECRET" not in out
    assert "https://host/x.m3u8" in out


def test_redact_strips_credentials_in_nested_structures():
    payload = {"url": "https://alice:hunter2@host/x?token=SECRET", "items": ["a", "b"]}
    out = redact(payload)
    assert "hunter2" not in str(out)
    assert out["url"] == "https://host/x"
