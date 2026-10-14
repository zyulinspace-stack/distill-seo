# Tech-audit www.distill-school.ru — 2026-10-14

**Краулинг:** 25 страниц · 2xx 24 · 3xx 0 · 4xx 0 · 5xx 0 · network errors 1

**Issues:** critical 0 · high 1 · medium 2 · low 0


## HIGH (1)


### network_error (1)

- `https://www.distill-school.ru/blog/` — HTTPSConnectionPool(host='www.distill-school.ru', port=443): Max retries exceeded with url: /blog/ (Caused by NameResolutionError("HTTPSConnection(host='www.distill-school.ru', port=443): Failed to resolve 'www.distill-school.ru' ([Errno -2] Name or service not known)"))

## MEDIUM (2)


### description_too_long (1)

- `https://www.distill-school.ru/axes/` — desc=213 символов, рекомендуется ≤160

### h1_multiple (1)

- `https://www.distill-school.ru/axes/` — найдено 7 <h1>, должен быть 1
