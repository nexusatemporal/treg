"""The GTM-engineering hub: a hand-written page that links the seven job pages, the workflows and
the public skills. It is only useful if every link on it resolves, it can be measured, and it
describes treg.to only where treg.to serves it."""

from __future__ import annotations

import json
import re

from httpx import ASGITransport, AsyncClient

from treg.api import app
from treg.config import get_settings

PATH = "/gtm-engineering"


async def test_hub_is_served_canonical_and_measurable(clients: AsyncClient):
    r = await clients.get(PATH)
    assert r.status_code == 200
    html = r.text
    assert '<link rel="canonical" href="' in html and f'{PATH}"/>' in html
    assert "{BASE}" not in html and "{ENDPOINTS}" not in html and "{PROVIDERS}" not in html
    # Hand-written pages are the ones PostHog sees; the hub exists partly to be measured.
    # The agent name in the H1 rotates in the browser; what a crawler reads must already be whole.
    assert '<span id="agName">Claude Code</span>' in html
    assert re.search(r"<h1>.*GTM engineering playbook.*Claude Code.*</h1>", html, re.S)
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert len(title.replace("&amp;", "&")) <= 62, title
    assert '<script src="/sitetrack.js"></script>' in html
    assert '<script src="/adtrack.js"></script>' in html
    kinds = []
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        kinds.append(json.loads(block)["@type"])
    assert kinds == ["BreadcrumbList", "Article", "FAQPage"]


async def test_every_internal_link_on_the_hub_resolves(clients: AsyncClient):
    """The hub is a page of links; a dead one is the failure nobody notices."""
    html = (await clients.get(PATH)).text
    # /app is the signed-in dashboard every Start button points at, not a page of this site.
    links = sorted({h for h in re.findall(r'href="(/[^"#?]*)', html)
                    if not h.startswith("//") and h != "/app"})
    assert "/workflows/find-and-verify-a-lead-list" in links and "/people-search" in links
    for href in links:
        r = await clients.get(href)
        assert r.status_code == 200, href


async def test_hub_is_linked_from_its_pillars_and_listed(clients: AsyncClient):
    for page in ("/", "/resources", "/people-search", "/leads-signals", "/blog", "/use-cases",
                 "/use-cases/lead-enrichment-for-ai-agents", "/workflows"):
        assert f'href="{PATH}"' in (await clients.get(page)).text, page
    assert f"{PATH}<" in (await clients.get("/sitemap.xml")).text
    # Hosted, the bundled pages and llms.txt keep the links with the markers unwrapped.
    for page in ("/resources", "/people-search", "/leads-signals", "/grokbot", "/fable",
                 "/use-cases/lead-enrichment-for-ai-agents", "/llms.txt"):
        r = await clients.get(page)
        assert r.status_code == 200 and "/gtm-engineering" in r.text, page
        assert "hosted-->" not in r.text, page


async def test_hub_404s_on_a_self_hosted_registry(monkeypatch):
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.example.internal")
    get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
            assert (await c.get(PATH)).status_code == 404
            assert (await c.get(PATH + ".md")).status_code == 404
            assert f"{PATH}<" not in (await c.get("/sitemap.xml")).text
            # Nothing served on a self-hosted registry may send a reader (or an agent) to the 404.
            for page in ("/", "/resources", "/people-search", "/leads-signals", "/grokbot", "/fable",
                         "/use-cases/lead-enrichment-for-ai-agents", "/llms.txt"):
                r = await c.get(page)
                assert r.status_code == 200, page
                assert "/gtm-engineering" not in r.text, page
                assert "hosted-->" not in r.text, page
    finally:
        get_settings.cache_clear()


async def test_markdown_twin_carries_every_chapter(clients: AsyncClient):
    """The Markdown copy is hand-kept; a chapter added to the page and not to the twin fails here."""
    import html as _html
    page = (await clients.get(PATH)).text
    md = await clients.get(PATH + ".md")
    assert md.status_code == 200
    assert md.headers.get("x-robots-tag") == "noindex"
    assert "{BASE}" not in md.text
    norm = lambda s: re.sub(r"[“”\"]", '"', _html.unescape(re.sub(r"<[^>]+>", "", s))).strip()
    headings = [norm(h) for h in re.findall(r'<h3 class="ct">(.*?)</h3>', page, re.S)]
    assert len(headings) >= 20
    md_headings = {norm(h) for h in re.findall(r"^## (.+)$", md.text, re.M)}
    missing = [h for h in headings if h not in md_headings]
    assert not missing, missing
    assert f'rel="alternate" type="text/markdown" href="' in page


async def test_studies_publish_results_without_naming_the_databases(clients: AsyncClient):
    """The three studies ship their headline figures on the page and the twin, and every FAQ in
    the JSON-LD is also visible. The job-change study names no catalog provider: the claim is about
    stored records against a live read, not a ranking of vendors."""
    import html as _html
    import json
    page = (await clients.get(PATH)).text
    md = (await clients.get(PATH + ".md")).text
    for anchor in ("job-changes", "test-your-signals", "found-vs-deliverable"):
        assert f'id="{anchor}"' in page and f'href="#{anchor}"' in page
    for figure in ("68%", "59%", "148", "53%", "32 deliverable", "26%", "54%"):
        assert figure in page, figure
    for figure in ("68%", "59%", "148", "32 deliverable (53%)", "| 26% | 54% |"):
        assert figure in md, figure
    start = page.index('id="job-changes"')
    md_start = md.index("### Job changes")
    study = (page[start:page.index('<ol class="play">', start)]
             + md[md_start:md.index("Rule:", md_start)]).lower()
    from pathlib import Path
    import treg
    catalog = Path(treg.__file__).parent / "catalog"
    providers = {p.name.split(".")[0] for p in catalog.glob("*.yaml")} - {"adapters", "linkedin", "you"}  # the source the study names, and a common word
    named = sorted(p for p in providers if re.search(rf"\b{re.escape(p)}\b", study))
    assert not named, named
    faq = next(json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
               if '"FAQPage"' in b)
    visible = {_html.unescape(q) for q in re.findall(r"<summary>(.*?)</summary>", page)}
    for q in ("How accurate is contact data after someone changes jobs?",
              "Do hiring or news signals predict that a startup is about to raise?"):
        assert q in visible and q in {e["name"] for e in faq["mainEntity"]}
