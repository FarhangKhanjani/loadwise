import pytest

from scripts.pair import PairError, app_url, check_key, pairing_link, tailscale_host


@pytest.mark.parametrize("given", [
    "https://my-pc.tail1234.ts.net", "https://my-pc.tail1234.ts.net/", "my-pc.tail1234.ts.net",
    "https://my-pc.tail1234.ts.net/app", "https://my-pc.tail1234.ts.net/app/",
])
def test_app_url_accepts_what_people_type(given):
    assert app_url(given) == "https://my-pc.tail1234.ts.net/app/"


def test_local_http_url_is_kept():
    assert app_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000/app/"


def test_key_goes_after_the_hash_so_it_is_never_sent_to_the_server():
    link = pairing_link("my-pc.ts.net", "abc_DEF-123+/=")
    assert link == "https://my-pc.ts.net/app/#key=abc_DEF-123%2B%2F%3D"
    assert "?" not in link


def test_tailscale_host_from_status_json():
    assert tailscale_host({"Self": {"DNSName": "my-pc.tail1234.ts.net."}}) == "my-pc.tail1234.ts.net"
    with pytest.raises(PairError):
        tailscale_host({"Self": {"DNSName": ""}})
    with pytest.raises(PairError):
        tailscale_host({})


@pytest.mark.parametrize("key", [None, "", "your-api-key"])
def test_check_key_rejects_missing_or_placeholder(key):
    with pytest.raises(PairError):
        check_key(key)


def test_check_key_accepts_a_real_length_key():
    assert check_key("x" * 32) == "x" * 32
