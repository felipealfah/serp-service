# serp-service

Scraper de SERP do Google, on-demand, exposto como API para o n8n. Recebe
`keyword` + `location` (cidade) e devolve orgânico / pago / Google Meu Negócio
em JSON. Localiza por cidade via UULE a partir de um único IP.

Serviço compartilhado — consumido por diferentes operações (prospector, leadgen)
pela API HTTP. Quem consome chama o endpoint; não precisa do código.

## Configuração

```bash
cp .env.example .env      # e defina uma API_KEY forte (openssl rand -hex 24)
```

## Subir

```bash
sudo docker compose up -d --build
sudo docker compose logs -f
```

Roda melhor num host com IP residencial. Docs interativas: `http://HOST:8000/docs`

## Testar

```bash
curl -X POST http://localhost:8000/serp \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"keyword":"caçamba de entulho","location":"São Paulo,State of São Paulo,Brazil"}'
```

## Request

| campo      | obrigatório | exemplo                                   |
|------------|-------------|-------------------------------------------|
| `keyword`  | sim         | `"caçamba de entulho"`                     |
| `location` | sim         | `"São Paulo,State of São Paulo,Brazil"` (nome canônico UULE) |
| `gl`       | não (br)    | `"br"`                                     |
| `hl`       | não (pt-BR) | `"pt-BR"`                                  |

## Response

```json
{
  "keyword": "...", "location": "...", "collected_at": "ISO-8601",
  "organic":   [{"pos":1,"domain":"...","title":"...","url":"..."}],
  "paid":      [{"pos":1,"domain":"...","title":"...","display_url":"..."}],
  "local_pack":[{"pos":1,"name":"...","domain":"...","rating":4.7,"reviews":"120"}]
}
```

- `429` = Google retornou `/sorry` (rate limit) — o cliente decide o retry.
- `401` = API key inválida.

## Notas

- `location` precisa ser o nome canônico UULE (`Cidade,State of Estado,Brazil`).
- `local_pack` sai por heurística da SERP — normalmente 3 resultados.
- Um IP residencial tem teto de volume; espace as chamadas.
