"""maps.py — Descoberta de negócios no Google Maps (para o Prospector).

Discovery only: navega o Maps, rola o feed para trazer volume, e extrai de cada
lugar nome, nota, avaliações, website e telefone. NÃO aplica filtros nem analisa
site — isso é regra de negócio do consumidor (Prospector).

Portado do prospectar_api.py (buscar_no_maps), que já rodava em produção — mesmos
seletores, mas usando o browser persistente do serviço (contexto novo por chamada).
"""

import asyncio
import re
from urllib.parse import quote

UA_DESKTOP = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _parse_nota(text: str) -> float | None:
    text = (text or "").strip().replace(",", ".")
    m = re.search(r"(\d+\.\d+|\d+)", text)
    return float(m.group(1)) if m else None


def _parse_avaliacoes(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


async def discover_maps(browser, nicho: str, cidade: str, max_results: int = 20) -> list[dict]:
    """Retorna [{name, rating, reviews, website, phone, maps_url}] do Google Maps."""
    query = f"{nicho} em {cidade}"
    maps_url = f"https://www.google.com/maps/search/{quote(query)}?hl=pt-BR"

    ctx = await browser.new_context(
        user_agent=UA_DESKTOP, locale="pt-BR", viewport={"width": 1280, "height": 900},
    )
    await ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    resultados: list[dict] = []
    try:
        page = await ctx.new_page()
        try:
            await page.goto(maps_url, wait_until="networkidle", timeout=30000)
        except Exception:
            await page.goto(maps_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Aceitar cookies
        try:
            botao = page.locator('button:has-text("Aceitar"), button:has-text("Accept")')
            if await botao.count() > 0:
                await botao.first.click()
                await asyncio.sleep(1)
        except Exception:
            pass

        # Rolar o feed até juntar lugares suficientes (ou chegar ao fim da lista)
        prev = 0
        stagnant = 0
        for _ in range(30):  # teto de rolagens
            try:
                await page.evaluate(
                    'const f = document.querySelector(\'[role="feed"]\'); if(f) f.scrollBy(0, 3000);'
                )
                await asyncio.sleep(1.5)
                if await page.locator('text="Você chegou ao fim da lista"').count() > 0:
                    break
                n = await page.evaluate(
                    '() => document.querySelectorAll(\'a[href*="/maps/place/"]\').length'
                )
                if n >= max_results:
                    break
                if n == prev:            # feed parou de crescer
                    stagnant += 1
                    if stagnant >= 3:
                        break
                else:
                    stagnant = 0
                prev = n
            except Exception:
                break

        # Coletar links de lugares
        hrefs: list[str] = await page.evaluate("""() => {
            const links = [];
            document.querySelectorAll('a[href*="/maps/place/"]').forEach(a => {
                if (a.href && !links.includes(a.href)) links.push(a.href);
            });
            return links;
        }""")
        hrefs = hrefs[:max_results]

        # Visitar cada lugar e extrair os dados
        for href in hrefs:
            try:
                await page.goto(href, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)

                nome = ""
                try:
                    nome = await page.locator("h1").first.text_content(timeout=5000) or ""
                except Exception:
                    pass
                if not nome:
                    continue

                nota = None
                try:
                    el = page.locator('[aria-label*="estrela"]').first
                    if await el.count() > 0:
                        nota = _parse_nota(await el.get_attribute("aria-label") or "")
                except Exception:
                    pass

                n_aval = 0
                try:
                    txt = await page.evaluate("() => document.body.innerText.substring(0, 3000)")
                    m = re.search(r"([\d.]+)\s*avalia[çc][oõ]es?", txt, re.IGNORECASE)
                    if m:
                        n_aval = _parse_avaliacoes(m.group(1))
                except Exception:
                    pass

                website = ""
                try:
                    w = page.locator('a[data-item-id="authority"]').first
                    if await w.count() > 0:
                        website = await w.get_attribute("href") or ""
                except Exception:
                    pass

                phone = ""
                try:
                    t = page.locator('a[href^="tel:"]').first
                    if await t.count() > 0:
                        phone = (await t.get_attribute("href") or "").replace("tel:", "").strip()
                except Exception:
                    pass

                resultados.append({
                    "name": nome.strip(),
                    "rating": nota,
                    "reviews": n_aval,
                    "website": website,
                    "phone": phone,
                    "maps_url": href,
                })
            except Exception:
                continue
    finally:
        await ctx.close()

    return resultados
