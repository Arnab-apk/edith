"""bot.actions.web: search parsing, SSRF guard, page reading (no network)."""
import socket

import httpx
import pytest

from bot.actions import web

SEARCH_HTML = """
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=x">Example <b>Page</b></a>
<a class="result__snippet" href="#">An example &amp; snippet</a></div>
<div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a></div>
<div class="result"><a class="result__a" href="https://other.org/">Other</a>
<a class="result__snippet">Second</a></div>
"""

PUBLIC = {"example.com": "93.184.216.34", "other.org": "93.184.216.35", "evil.test": "93.184.216.36",
          "router.test": "192.168.1.1", "localhost": "127.0.0.1"}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    def getaddrinfo(host, *a, **k):
        if host not in PUBLIC:
            raise socket.gaierror("unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC[host], 0))]
    monkeypatch.setattr(web.socket, "getaddrinfo", getaddrinfo)
    web._RESULTS.clear()


def use_transport(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(web.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_parse_results_decodes_redirect_and_skips_ads():
    res = web.parse_results(SEARCH_HTML, 5)
    assert [r["url"] for r in res] == ["https://example.com/page", "https://other.org/"]
    assert res[0]["title"] == "Example Page" and res[0]["snippet"] == "An example & snippet"


async def test_web_search_remembers_result_urls(monkeypatch):
    use_transport(monkeypatch, lambda req: httpx.Response(200, text=SEARCH_HTML))
    r = await web.web_search("example")
    assert r.ok and "example.com/page" in r.text and r.text.startswith(web.DATA_NOTE)
    assert web.is_search_result("https://example.com/page")
    assert not web.is_search_result("https://evil.test/?q=secret")


@pytest.mark.parametrize("url", ["http://localhost:8080/", "http://router.test/admin", "file:///C:/x",
                                 "ftp://example.com/", "https://no-such-host.test/"])
def test_check_url_refuses_local_and_odd_links(url):
    assert web.check_url(url)


async def test_read_webpage_strips_html(monkeypatch):
    page = ("<html><head><title>Hi &amp; bye</title><style>x{}</style></head><body><nav>menu</nav>"
            "<p>First para</p><script>alert(1)</script><p>Second</p></body></html>")
    use_transport(monkeypatch, lambda req: httpx.Response(200, text=page, headers={"content-type": "text/html"}))
    r = await web.read_webpage("https://example.com/")
    assert r.ok and "Hi & bye" in r.text and "First para\nSecond" in r.text
    assert "alert" not in r.text and "menu" not in r.text


async def test_redirect_to_private_address_is_refused(monkeypatch):
    def handler(req):
        if req.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://router.test/admin"})
        raise AssertionError("must not fetch the private host")
    use_transport(monkeypatch, handler)
    r = await web.read_webpage("https://example.com/")
    assert not r.ok and "private" in r.text


async def test_read_webpage_truncates(monkeypatch):
    use_transport(monkeypatch, lambda req: httpx.Response(200, text="a" * 5000, headers={"content-type": "text/plain"}))
    r = await web.read_webpage("https://example.com/x.txt", max_chars=1000)
    assert r.ok and "truncated" in r.text and r.text.count("a") < 1100
