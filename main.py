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

from maps import discover_maps

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

    // === Pago: blocos [data-text-ad] (top e bottom). O destino real vem de
    //     data-pcu — o href do anúncio é um redirect /aclk do próprio Google. ===
    document.querySelectorAll('[data-text-ad]').forEach(ad => {
        if (out.paid.length >= caps.paid) return;
        const a = ad.querySelector('a[data-pcu], a.sVXRqc, a[href]');
        if (!a) return;
        let dest = a.getAttribute('data-pcu') || '';
        if (!dest) {
            const m = (a.href || '').match(/[?&]adurl=([^&]+)/);
            dest = m ? decodeURIComponent(m[1]) : a.href;
        }
        const d = domain(dest);
        if (!d || d.includes('google') || seen.has('ad:' + d)) return;
        seen.add('ad:' + d);
        const h = ad.querySelector('[role="heading"], h3');
        const cite = ad.querySelector('cite');
        out.paid.push({pos: out.paid.length + 1, domain: d,
            title: h ? h.innerText.trim() : (a.innerText || '').trim().split('\n')[0],
            display_url: cite ? cite.innerText.trim() : d});
    });

    // === Google Meu Negócio (local pack): blocos .rllt__details, fora do #rso ===
    document.querySelectorAll('.rllt__details').forEach(el => {
        if (out.local_pack.length >= caps.local) return;
        const nameEl = el.querySelector('.OSrXXb, .dbg0pd, [role="heading"]');
        const name = nameEl ? nameEl.innerText.trim() : '';
        if (!name) return;
        const ratingEl = el.querySelector('.yi40Hd');
        const revEl = el.querySelector('.RDApEe');
        const phone = ((el.innerText || '').match(/\(?\d{2}\)?\s?9?\d{4}-?\d{4}/) || [''])[0];
        out.local_pack.push({
            pos: out.local_pack.length + 1,
            name: name,
            rating: ratingEl ? parseFloat(ratingEl.innerText.replace(',', '.')) : null,
            reviews: revEl ? revEl.innerText.replace(/[()]/g, '').trim() : '',
            phone: phone,
        });
    });

    // === Orgânico: #rso ===
    let orgPos = 0;
    document.querySelectorAll('#rso a[href^="http"]').forEach(a => {
        if (orgPos >= caps.organic || !a.querySelector('h3')) return;
        const d = domain(a.href);
        if (!d || d.includes('google') || seen.has(d)) return;
        seen.add(d);
        orgPos++;
        out.organic.push({pos: orgPos, domain: d, title: a.querySelector('h3').innerText.trim(), url: a.href});
    });

    return out;
}"""

CAPS = {"organic": 10, "paid": 4, "local": 5}

# Diagnóstico do DOM — revela o que existe na SERP pra acertar os seletores.
_DEBUG_JS = r"""() => {
    const q = s => document.querySelectorAll(s).length;
    const info = {
        rso: q('#rso'),
        tads: q('#tads'),
        tads_ads: q('#tads [data-text-ad], #tads .uEierd'),
        bottomads: q('#bottomads, #tadsb'),
        data_text_ad_total: q('[data-text-ad]'),
        rllt_details: q('.rllt__details'),
        vkpgbb: q('.VkpGBb'),
        more_places: !!Array.from(document.querySelectorAll('a,div,span,g-more-link'))
            .find(e => /mais lugares|more places/i.test(e.innerText || '')),
    };
    const firstAd = document.querySelector('#tads [data-text-ad], #tads .uEierd, [data-text-ad]');
    info.sample_ad = firstAd ? firstAd.outerHTML.slice(0, 1200) : null;
    const firstLocal = document.querySelector('.rllt__details, .VkpGBb');
    info.sample_local = firstLocal ? firstLocal.outerHTML.slice(0, 1200) : null;
    const firstBottom = document.querySelector('#tadsb, #bottomads');
    info.sample_bottomad = firstBottom ? firstBottom.outerHTML.slice(0, 1800) : null;
    return info;
}"""


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
    debug: bool = False  # retorna diagnóstico do DOM em vez dos resultados


async def _run_search(browser, keyword: str, location: str, gl: str, hl: str,
                      debug: bool = False) -> dict | None:
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
        if debug:
            return {"__debug__": await page.evaluate(_DEBUG_JS)}
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
        results = await _run_search(app.state.browser, req.keyword, req.location,
                                    req.gl, req.hl, req.debug)

    if results is None:
        # /sorry — deixa o n8n decidir o retry/backoff
        raise HTTPException(status_code=429, detail="Google retornou /sorry (rate limit). Tente novamente depois.")

    if req.debug:
        return {"keyword": req.keyword, "location": req.location, **results}

    return {
        "keyword": req.keyword,
        "location": req.location,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "organic": results["organic"],
        "paid": results["paid"],
        "local_pack": results["local_pack"],
    }


class MapsRequest(BaseModel):
    nicho: str = Field(..., description="Segmento/nicho, ex: 'dentistas'")
    cidade: str = Field(..., description="Cidade, ex: 'Brasília/DF'")
    max_results: int = Field(20, ge=1, le=60, description="Máximo de lugares a extrair")


@app.post("/maps")
async def maps_endpoint(req: MapsRequest, x_api_key: str | None = Header(default=None)):
    """Descoberta no Google Maps (para o Prospector). Só extração — sem filtros.

    Retorna negócios com name, rating, reviews, website, phone, maps_url.
    Os filtros de qualificação (nota mínima, site ruim, e-mail) ficam no consumidor.
    """
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")

    async with app.state.sem:
        businesses = await discover_maps(app.state.browser, req.nicho, req.cidade, req.max_results)

    return {
        "nicho": req.nicho,
        "cidade": req.cidade,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "count": len(businesses),
        "businesses": businesses,
    }
