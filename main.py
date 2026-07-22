"""main.py — SERP API (FastAPI + Playwright async). Só scrap + resposta JSON.

O n8n faz POST /serp com {keyword, location} e recebe na hora:
  - organic:    top 5 (busca orgânica)
  - paid:       top 4 (anúncios)
  - local_pack: top 5 (Google Meu Negócio)

Roda no IP residencial nativo do NAS. Browser persistente (1 Chromium),
contexto novo por request (cookies isolados), concorrência limitada.
Persistência (BigQuery etc.) é responsabilidade do n8n — aqui é só scrap.

Env:
  API_KEY          — se setado, exige header X-API-Key (recomendado)
  MAX_CONCURRENCY  — buscas simultâneas (default 2 — cuidado com RAM e /sorry)
"""

import asyncio
import base64
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import quote_plus

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

API_KEY = os.environ.get("API_KEY", "")


def uule_v2(name: str) -> str:
    """UULE robusto (protobuf) — localiza a busca por cidade de um único IP."""
    b = name.encode("utf-8")
    payload = b"\x08\x02\x10\x20\x22" + bytes([len(b)]) + b
    return "w+" + base64.b64encode(payload).decode()


_SERP_JS = r"""(caps) => {
    const out = {organic: [], local_pack: [], paid: []};
    const seen = new Set();
    function domain(url){ try { return new URL(url).hostname.replace(/^www\./,''); } catch(e){ return ''; } }

    // Pago — top N
    (document.querySelectorAll('#tads [data-text-ad], #tads .uEierd') || []).forEach((ad, idx) => {
        if (out.paid.length >= caps.paid) return;
        const link = ad.querySelector('a[href]'); const h3 = ad.querySelector('h3, [role="heading"]');
        const cite = ad.querySelector('cite'); if (!link) return;
        const d = domain(link.href); if (!d || d.includes('google')) return;
        out.paid.push({pos: out.paid.length+1, domain: d, title: h3?.innerText?.trim()||'', display_url: cite?.innerText?.trim()||''});
    });

    // Orgânico + local pack (heurística por rating no bloco)
    let orgPos = 0;
    document.querySelectorAll('#rso a[href^="http"]').forEach(a => {
        if (!a.querySelector('h3')) return;
        const d = domain(a.href); if (!d || d.includes('google') || seen.has(d)) return;
        const block = a.closest('[data-hveid], .g, li, [jsaction]');
        const rating = block?.querySelector('.MW4etd, .yi40Hd, [aria-label*="estrela"], [aria-label*="star"]');
        seen.add(d);
        if (rating && out.local_pack.length < caps.local) {
            const name = block.querySelector('[role="heading"], h3');
            const rev = block.querySelector('.UY7F9, .RDApEe, .F7nice');
            out.local_pack.push({pos: out.local_pack.length+1, name: name?.innerText?.trim()||d, domain: d,
                rating: parseFloat(rating.innerText.replace(',','.'))||null, reviews: rev?.innerText?.trim()||''});
        } else if (!rating && orgPos < caps.organic) {
            orgPos++;
            out.organic.push({pos: orgPos, domain: d, title: a.querySelector('h3').innerText.trim(), url: a.href});
        }
    });
    return out;
}"""

CAPS = {"organic": 5, "paid": 4, "local": 5}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled", "--disable-quic"],
    )
    app.state.pw = pw
    app.state.browser = browser
    app.state.sem = asyncio.Semaphore(int(os.environ.get("MAX_CONCURRENCY", "2")))
    yield
    await browser.close()
    await pw.stop()


app = FastAPI(title="SERP API (NAS)", lifespan=lifespan)


class SerpRequest(BaseModel):
    keyword: str = Field(..., description="Palavra-chave da busca")
    location: str = Field(..., description='Nome canônico UULE, ex: "São Paulo,State of São Paulo,Brazil"')
    gl: str = "br"
    hl: str = "pt-BR"


async def _run_search(browser, keyword: str, location: str, gl: str, hl: str) -> dict | None:
    ctx = await browser.new_context(locale=hl, timezone_id="America/Sao_Paulo", user_agent=UA)
    try:
        page = await ctx.new_page()
        url = (f"https://www.google.com/search?q={quote_plus(keyword)}"
               f"&hl={hl}&gl={gl}&uule={uule_v2(location)}&num=20")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if "/sorry" in page.url:
            return None
        try:
            await page.wait_for_selector("#rso, #search", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        return await page.evaluate(_SERP_JS, CAPS)
    finally:
        await ctx.close()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/serp")
async def serp(req: SerpRequest, x_api_key: str | None = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")

    async with app.state.sem:
        results = await _run_search(app.state.browser, req.keyword, req.location, req.gl, req.hl)

    if results is None:
        # /sorry — deixa o n8n decidir o retry/backoff
        raise HTTPException(status_code=429, detail="Google retornou /sorry (rate limit). Tente novamente depois.")

    return {
        "keyword": req.keyword,
        "location": req.location,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "organic": results["organic"],
        "paid": results["paid"],
        "local_pack": results["local_pack"],
    }
