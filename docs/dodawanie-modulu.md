# Jak dodać moduł dla kolejnego systemu

Moduł łączy jeden zewnętrzny system (wFirma, inFakt, iFirma, bank, ...) z serwerem. Ten
przewodnik opiera się na module Fakturowni, który jest najmniejszym kompletnym przykładem:

```
src/firma_mcp/modules/fakturownia/
  client.py     rozmawia z API systemu i zwraca zwykłe słowniki
  __init__.py   czyta konfigurację i rejestruje narzędzia
tests/modules/test_fakturownia.py
```

English version: [adding-a-module.md](adding-a-module.md)

## Kontrakt

Moduł to obiekt `Module` (patrz `src/firma_mcp/modules/__init__.py`) z polami:

| Pole | Znaczenie |
| --- | --- |
| `name` | Krótki identyfikator, zarazem przedrostek nazw narzędzi |
| `status` | `PRODUCTION` albo `EXPERIMENTAL`. Uczciwie: produkcja oznacza pracę z prawdziwym systemem |
| `summary` | Jedno zdanie dopisywane do instrukcji, które widzi asystent |
| `load(env)` | Czyta zmienne środowiskowe i zwraca konfigurację albo `None`, gdy moduł nie jest skonfigurowany |
| `register(mcp, config, ctx)` | Dodaje narzędzia |
| `requires` | Opcjonalnie nazwy modułów, od których ten zależy |

Serwer włącza moduł dokładnie wtedy, gdy `load` zwróci konfigurację.

## Krok 1: klient

Napisz klasę, która wywołuje API i zwraca zwykłe słowniki. Nie umieszczaj w niej niczego
związanego z MCP, żeby dało się ją testować osobno. Zasady, których trzymają się istniejące
klienty:

- **Tylko odczyt.** Wysyłaj wyłącznie żądania, które nie mogą zmienić danych. Klient
  Fakturowni wysyła tylko `GET`, a test nie przejdzie, jeśli w pliku pojawi się inna metoda
  HTTP.
- **Każdy błąd zgłaszaj jako `SourceError`**: błędy sieci, błędy HTTP, nieoczekiwany kształt
  odpowiedzi. Nic innego nie powinno wydostać się na zewnątrz.
- **Wszystko ograniczaj**: limit czasu, limit stron, limit rozmiaru treści binarnych.
- **Nigdy nie wstawiaj danych wejściowych narzędzia do adresu URL bez sprawdzenia.**
  Przykładem jest `_numeric_id`.
- **Kwoty zamieniaj na napisy dziesiętne** w rodzaju `"123.45"`, jak w pozostałych modułach.
- Przyjmuj w konstruktorze transport `httpx`, żeby testy mogły podstawić zaślepkę HTTP.

## Krok 2: konfiguracja

`load` dostaje środowisko i decyduje, czy moduł jest włączony. `read_group` realizuje regułę
obowiązującą wszędzie: wszystkie zmienne ustawione oznaczają moduł włączony, żadna oznacza
wyłączony, a tylko część to błąd, który zatrzymuje serwer przy starcie.

```python
def load(env):
    values = read_group(env, "fakturownia", ("FAKTUROWNIA_DOMAIN", "FAKTUROWNIA_API_TOKEN"))
    if values is None:
        return None
    return FakturowniaConfig(domain=values["FAKTUROWNIA_DOMAIN"], api_token=values["FAKTUROWNIA_API_TOKEN"])
```

Sprawdź tutaj, co się da (błędna domena, nieznana nazwa środowiska), i zgłoś `ConfigError`.
Złe ustawienie powinno wyjść przy starcie, a nie przy pierwszym wywołaniu narzędzia.

Każdą nową zmienną dopisz do `.env.example` z informacją, skąd wziąć jej wartość.

Prawdziwy moduł Fakturowni ma jeszcze jeden warunek ponad ten przykład: ponieważ jest
eksperymentalny, wymaga dodatkowo `FAKTUROWNIA_ENABLED=true`. Rozważ to samo dla każdego
modułu, który nie działał jeszcze na żywym koncie.

## Krok 3: narzędzia

`register` tworzy klienta i deklaruje narzędzia. Liczą się trzy rzeczy:

```python
def register(mcp, config, ctx):
    client = ctx.overrides.get("fakturownia") or FakturowniaClient(config.domain, config.api_token)
    ctx.services["fakturownia"] = client

    @mcp.tool(description="Sales invoices issued in the last `days` days ...", annotations=READ_ONLY)
    def fakturownia_sales(days: int = 7, unpaid_only: bool = False) -> dict:
        d = clamp_days(days, default=7)
        return ctx.run("fakturownia_sales", lambda: listing(client.sales(d, unpaid_only)))
```

1. **`ctx.run(name, fn)`** opakowuje każde wywołanie. Sprawdza, czy wywołujący ma dostęp,
   loguje wywołanie bez argumentów i zamienia `SourceError` na `{"error": ..., "source": ...}`,
   dzięki czemu awaria jednego systemu nie psuje pozostałych.
2. **`annotations=READ_ONLY`** mówi Claude i ChatGPT, że narzędzie niczego nie zmienia.
3. **`ctx.overrides`** pozwala testom podać atrapę klienta. **`ctx.services`** udostępnia
   Twojego klienta modułom rejestrowanym po Twoim.

Opis narzędzia pisz dla asystenta: co narzędzie zwraca, co znaczą parametry i jakie mają
ograniczenia, kiedy go użyć. Nazwy narzędzi poprzedzaj nazwą modułu.

## Krok 4: rejestracja modułu

Wyeksportuj `MODULE` i dopisz go do `all_modules()` w `src/firma_mcp/modules/__init__.py`:

```python
MODULE = Module(name="fakturownia", status=EXPERIMENTAL, summary="...", load=load, register=register)
```

Kolejność w `all_modules()` to kolejność rejestracji. Moduł korzystający z klienta innego
modułu musi być po nim. Narzędzie potrzebujące dwóch systemów jest osobnym małym modułem
z polem `requires`; przykładem jest `booking.py` (KSeF plus Merit).

## Krok 5: testy

Testuj na zaślepkach odpowiedzi HTTP przez `httpx.MockTransport`; wzorem jest
`tests/modules/test_fakturownia.py`. Sprawdź co najmniej:

- wysyłane żądanie (ścieżka, parametry, nagłówek uwierzytelnienia, brak sekretów w adresie),
- mapowanie udokumentowanej odpowiedzi na Twój kształt wyniku,
- stronicowanie i jego limit,
- zamianę każdego rodzaju awarii na `SourceError`,
- `load` dla modułu skonfigurowanego, nieskonfigurowanego i skonfigurowanego częściowo,
- to, że klient nie potrafi zapisywać.

Używaj wyłącznie zmyślonych danych. Nigdy nie wklejaj do testu prawdziwej odpowiedzi
z własnego konta.

Zaślepki dowodzą, że kod zgadza się z dokumentacją, a nie że zgadza się z prawdziwym
systemem. Dopóki moduł nie zadziałał na żywym koncie, oznacz go jako `EXPERIMENTAL` i napisz
to w README.

## Lista kontrolna

- [ ] Klient jest tylko do odczytu, ma ograniczenia i zgłasza wyłącznie `SourceError`
- [ ] `load` zwraca `None` bez konfiguracji i zgłasza `ConfigError` przy konfiguracji częściowej
- [ ] Każde narzędzie przechodzi przez `ctx.run` i ma `annotations=READ_ONLY`
- [ ] Zmienne opisane w `.env.example`
- [ ] Moduł dopisany do `all_modules()` i do tabeli statusów w obu README
- [ ] Testy używają tylko zmyślonych danych; `pytest` i `ruff check .` przechodzą
