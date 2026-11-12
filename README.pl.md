# firma-mcp

Serwer [Model Context Protocol](https://modelcontextprotocol.io) tylko do odczytu, dzięki
któremu asystent AI może zajrzeć do zaplecza małej polskiej firmy: faktur w KSeF, księgowości
w Merit Aktiva, firmowej skrzynki pocztowej oraz (eksperymentalnie) Fakturowni.

Uruchamiasz go na własnym serwerze, podajesz własne dane dostępowe i dodajesz do Claude albo
ChatGPT jako zdalny łącznik. Jedna instalacja obsługuje jedną firmę.

English version: [README.md](README.md)

## Do czego to służy

Powstało na potrzeby codziennego „porannego briefu”: asystent czyta nową pocztę, faktury,
które wpłynęły do KSeF, oraz to, co jest zaksięgowane i zapłacone w systemie księgowym,
a potem zgłasza, czym trzeba się zająć. Każde narzędzie wyłącznie czyta. Nic tutaj nie potrafi
wysłać wiadomości, wystawić faktury ani niczego zmienić lub usunąć w podłączonych systemach.

## Obsługiwane systemy

| System | Moduł | Status | Co to znaczy |
| --- | --- | --- | --- |
| KSeF 2.0 (Krajowy System e-Faktur) | `ksef` | Produkcja | Działa codziennie na produkcyjnym API KSeF w firmie autora. |
| Merit Aktiva / 360 Księgowość | `merit` | Produkcja | Działa codziennie na polskim adresie API. Adresy estoński i fiński nie były testowane. |
| Poczta (IMAP) | `mail` | Produkcja | Działa codziennie z jednym serwerem IMAP (TLS, port 993, hasła aplikacyjne). Inne serwery nie były testowane. |
| Fakturownia | `fakturownia` | **Eksperymentalny, domyślnie wyłączony** | Napisany na podstawie publicznej dokumentacji API. Testowany wyłącznie na zaślepkach odpowiedzi, nigdy na żywym koncie. |

„Produkcja” opisuje instalację w jednej firmie. Nie jest to stwierdzenie o czyjejkolwiek
innej.

Moduł włącza się tylko wtedy, gdy ma komplet ustawień. Reszta serwera działa bez niego.

Wyjątkiem jest Fakturownia: jest domyślnie wyłączona. Samo wpisanie danych dostępowych jej
nie uruchamia. Trzeba ją świadomie włączyć osobnym ustawieniem `FAKTUROWNIA_ENABLED=true`,
bo moduł nigdy nie działał na żywym koncie. Jeśli przełącznik jest włączony, a danych
dostępowych brakuje, serwer odmawia startu.

## Narzędzia

| Narzędzie | Moduł | Co zwraca |
| --- | --- | --- |
| `mail_list` | mail | Ostatnie wiadomości w skrzynce: nadawca, temat, data, skrót treści, nazwy załączników |
| `mail_get` | mail | Pełna treść jednej wiadomości |
| `mail_attachment` | mail | Tekst załącznika PDF, DOCX lub TXT albo surowe bajty w base64 |
| `merit_purchases` | merit | Zaksięgowane faktury zakupu, opcjonalnie tylko niezapłacone |
| `merit_sales` | merit | Wystawione faktury sprzedaży, opcjonalnie tylko niezapłacone |
| `merit_invoice` | merit | Szczegóły jednej faktury, opcjonalnie z PDF |
| `merit_payments` | merit | Zarejestrowane wpłaty i wypłaty |
| `payment_check` | merit | Czy płatność o danej kwocie już zaszła |
| `ksef_received` | ksef | Faktury przyjęte w KSeF; przy włączonym Merit także informacja, czy są zaksięgowane |
| `ksef_invoice` | ksef | XML jednej faktury z KSeF |
| `booking_suggestions` | ksef + merit | Dla niezaksięgowanych faktur z KSeF: konto i schemat VAT używane wcześniej u tego samego sprzedawcy |
| `fakturownia_sales` | fakturownia | Faktury sprzedaży (eksperymentalne) |
| `fakturownia_purchases` | fakturownia | Faktury kosztowe (eksperymentalne) |
| `fakturownia_invoice` | fakturownia | Szczegóły jednej faktury, opcjonalnie z PDF (eksperymentalne) |
| `fakturownia_payment_status` | fakturownia | Stan płatności jednej faktury: zapłacono, pozostało, czy po terminie (eksperymentalne) |

Wszystkie narzędzia mają adnotację MCP `readOnlyHint: true`. Skrzynka jest otwierana
poleceniem IMAP `EXAMINE`, a wiadomości pobierane bez oznaczania jako przeczytane.

Opisy narzędzi i komunikaty błędów są po angielsku; asystent i tak odpowiada w języku
rozmowy. Część zwracanych wartości jest po polsku, bo opisują polską księgowość i korzystają
z nich istniejące klienty: kierunki płatności (`wpływ`, `wypływ`, `kompensata`, `nieznany`)
oraz notatka dla sprzedawcy bez historii (`nowy kontrahent — brak historii`). Dane
z podłączonych systemów są przekazywane w takim języku, w jakim zostały zapisane.

## Uruchomienie przez Docker Compose

Potrzebujesz maszyny z Dockerem, domeny wskazującej na tę maszynę oraz otwartych portów 80
i 443.

```bash
git clone https://github.com/zacharm-ewsoft/firma-mcp.git
cd firma-mcp
cp .env.example .env
chmod 600 .env
# uzupełnij .env: patrz „Konfiguracja” niżej
docker compose up -d
docker compose logs -f firma-mcp
```

Plik Compose uruchamia dwa kontenery: serwer oraz [Caddy](https://caddyserver.com), który sam
uzyskuje i odnawia certyfikat Let's Encrypt dla `FIRMA_DOMAIN`. Serwer nie jest wystawiony na
hosta; wystawiony jest tylko Caddy.

Po poprawnym starcie log pokazuje włączone moduły:

```
INFO firma_mcp module enabled: ksef (production)
INFO firma_mcp starting: auth=github modules=ksef url=https://mcp.example.com/mcp
```

Jeśli konfiguracja jest niepełna, serwer wypisuje, czego brakuje, i kończy pracę z kodem 2.

Wolisz bez Dockera? W katalogu `deploy/` jest przykładowa konfiguracja nginx i jednostka
systemd. Tak działa instalacja autora.

## Konfiguracja

Wszystko ustawia się zmiennymi środowiskowymi. Plik [.env.example](.env.example) zawiera
każdą zmienną z informacją, skąd wziąć jej wartość. W skrócie:

1. **Adres serwera**: `FIRMA_BASE_URL` (np. `https://mcp.example.com`) i `FIRMA_DOMAIN`
   (ten sam host, używany przez Caddy).
2. **Uwierzytelnianie** (wymagane, patrz niżej).
3. **Moduły**: uzupełnij sekcje systemów, z których korzystasz, pozostałe zostaw puste.
   Dla eksperymentalnego modułu Fakturowni ustaw dodatkowo `FAKTUROWNIA_ENABLED=true`.

### Uwierzytelnianie

Nie ma trybu bez uwierzytelnienia. Bez poprawnych ustawień serwer odmawia startu.

**Logowanie przez GitHub (domyślne, potrzebne dla claude.ai i ChatGPT).** Oba asystenty łączą
się ze zdalnymi serwerami MCP przez OAuth, więc serwer potrzebuje dostawcy tożsamości. Używa
aplikacji OAuth w GitHubie, którą zakładasz samodzielnie:

1. W GitHubie otwórz **Settings > Developer settings > OAuth Apps > New OAuth App**.
2. W polu **Homepage URL** wpisz swój `FIRMA_BASE_URL`, a w **Authorization callback URL**
   `<FIRMA_BASE_URL>/auth/callback`.
3. Skopiuj identyfikator klienta i nowy sekret do `GITHUB_CLIENT_ID` i
   `GITHUB_CLIENT_SECRET`.
4. Wpisz swoją nazwę użytkownika GitHub w `FIRMA_ALLOWED_GITHUB_LOGINS` (kilka nazw
   oddziel przecinkami). Logowanie może przejść każdy, kto ma konto GitHub; dopiero ta lista,
   sprawdzana przy każdym wywołaniu narzędzia, ogranicza dostęp do Ciebie. Konto spoza listy
   dostaje odmowę, a z pustą listą serwer nie startuje. Logowanie prosi GitHuba wyłącznie
   o zakres `read:user`.
5. Wygeneruj `FIRMA_JWT_SIGNING_KEY` poleceniem `openssl rand -hex 32`.

**Wspólny token (`FIRMA_AUTH_MODE=token`), opcjonalnie.** Jeden długi sekret wysyłany jako
`Authorization: Bearer <token>`. Służy do testów i własnych skryptów, a nie do asystentów:
Claude i ChatGPT podłączaj przez opisane wyżej logowanie GitHub. ChatGPT w ogóle nie skorzysta
z tokenu, a claude.ai obsługuje nagłówki żądań tylko w ograniczonej wersji beta. Token krótszy
niż 32 znaki serwer odrzuca już przy starcie.

## Podłączenie do Claude

Kroki według dokumentacji Anthropic
([Add a connector that isn't in the directory](https://claude.com/docs/connectors/custom/add-unlisted)),
stan na listopad 2026. Nazwy przycisków podaję tak, jak w dokumentacji (po angielsku).

1. W Claude otwórz **Customize > Connectors**. W planach Team i Enterprise łącznik najpierw
   dodaje właściciel organizacji w **Organization settings > Connectors**.
2. Wybierz **Add**, potem **Add custom connector**.
3. Podaj nazwę i adres `https://<twoja domena>/mcp`, potem **Continue**.
4. Zostaw ustawienia uwierzytelniania oznaczone jako **Detected** i kliknij **Add**.
5. Kliknij **Connect** i zaloguj się kontem GitHub, które jest na liście dozwolonych.
6. W rozmowie użyj przycisku **+** i pozycji **Connectors**, aby włączyć łącznik.

Claude Code może też połączyć się wspólnym tokenem (do testów):

```bash
claude mcp add --transport http firma https://<twoja domena>/mcp \
  --header "Authorization: Bearer <FIRMA_AUTH_TOKEN>"
```

## Podłączenie do ChatGPT

Kroki według dokumentacji OpenAI
([Connect and test your plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt)),
stan na listopad 2026. O tym, czy możesz dodawać własne serwery MCP, decyduje Twój plan
i ustawienia przestrzeni roboczej.

1. Otwórz [chatgpt.com/plugins](https://chatgpt.com/plugins).
2. Wybierz przycisk plus, potem **Add custom MCP server**.
3. Podaj nazwę i opis.
4. W sekcji **Connection** wpisz `https://<twoja domena>/mcp`.
5. Skonfiguruj uwierzytelnianie (OAuth), przeczytaj ostrzeżenie o ryzyku i wybierz
   **I understand and want to continue**.
6. Wybierz **Create as a plugin**, przejrzyj wykryte narzędzia i zaloguj się przez GitHub,
   gdy pojawi się taka prośba.
7. W nowej rozmowie wpisz `@` i wybierz wtyczkę.

Dokumentacja OpenAI stwierdza, że własne serwery MCP nie muszą mieć narzędzi `search`
i `fetch`, więc ten serwer ich nie dodaje. Są wymagane tylko wtedy, gdy serwer ma być
źródłem dla funkcji „company knowledge”.

## Co zostało naprawdę sprawdzone

> **Stan obecny.** Ten kod został przebudowany z prywatnej instalacji do modułowej postaci,
> którą tu widać. Po tej przebudowie **nie był jeszcze** uruchomiony na żywych systemach
> (KSeF, Merit, prawdziwa skrzynka) i **nie był jeszcze** podłączony ani do Claude, ani do
> ChatGPT. Wszystko poniżej sprawdzono wyłącznie testami automatycznymi i uruchomieniami
> lokalnymi. Ta uwaga zostanie zaktualizowana po testach na żywo.

Celowo podane osobno.

**Testy automatyczne (uruchamiane w CI przy każdym wypchnięciu).** 237 testów jednostkowych.
KSeF, Merit i Fakturownia są testowane na zaślepkach odpowiedzi HTTP, a poczta na atrapie
skrzynki IMAP. Wszystkie dane testowe są zmyślone.

**MCP Inspector.** Oficjalny [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector)
(wersja 2.15.0, tryb CLI) został uruchomiony na lokalnej instancji w trybie tokenu: wylistował
wszystkie 15 narzędzi z adnotacjami tylko do odczytu i poprawnie wywołał narzędzie. Bez tokenu
dostał odmowę.

**Powierzchnia OAuth, lokalnie.** Lokalną instancję w trybie GitHub sprawdzono zwykłymi
żądaniami HTTP. Żądanie bez uwierzytelnienia dostaje `401` z nagłówkiem `WWW-Authenticate`
wskazującym metadane chronionego zasobu. Metadane serwera autoryzacji ogłaszają PKCE `S256`,
dynamiczną rejestrację klienta i dokumenty metadanych klienta. Rejestracja udaje się dla
adresów zwrotnych Claude (`https://claude.ai/api/mcp/auth_callback`) i ChatGPT
(`https://chatgpt.com/connector_platform_oauth_redirect`, `https://chatgpt.com/connector/oauth/...`),
a dla innych hostów jest odrzucana. Test używał fikcyjnego klienta GitHub, więc samo przejście
przez GitHub nie było jego częścią.

**Claude.** Prywatna instalacja, z której ten projekt został wyodrębniony, jest używana
codziennie jako własny łącznik w claude.ai, z tym samym komponentem logowania przez GitHub
i tą samą logiką KSeF, Merit i poczty. Przebudowany kod z tego repozytorium nie był jeszcze
sam podłączony do claude.ai.

**ChatGPT.** Nie testowano na żywym koncie ChatGPT. Zgodność opiera się na wymaganiach
opisanych w dokumentacji OpenAI (Streamable HTTP pod `/mcp`, OAuth 2.1 z PKCE i rejestracją
klienta, adnotacje tylko do odczytu) oraz na lokalnym teście OAuth opisanym wyżej.

**Docker Compose.** Obraz się buduje, a zestaw dwóch kontenerów został uruchomiony lokalnie:
Caddy obsługiwał HTTPS z lokalnym certyfikatem i przekazywał ruch do serwera. Uzyskanie
prawdziwego certyfikatu Let's Encrypt na publicznej domenie nie było testowane.

**Nie testowano.** Fakturowni na żywym koncie. Merit poza Polską. Serwerów IMAP innych niż
ten używany. Pracy z wieloma skrzynkami ani z bardzo dużymi kontami.

## Uwagi o bezpieczeństwie

- **Tylko odczyt z założenia.** Fasada Merit udostępnia wyłącznie wywołania odczytu, klient
  Fakturowni wysyła wyłącznie `GET`, a testy nie przejdą, jeśli w którymś z nich pojawi się
  wywołanie zapisu. Same dane dostępowe do systemów mogą jednak pozwalać na zapis, więc
  przechowuj je starannie i tam, gdzie system na to pozwala, używaj ograniczonych uprawnień
  (KSeF pozwala: nadaj tokenowi tylko uprawnienie do przeglądania faktur).
- **Uwierzytelnianie jest obowiązkowe**, a przy niepełnej konfiguracji serwer nie startuje.
- **Sekrety są tylko w `.env`.** Trzymaj ten plik poza kontrolą wersji (`.gitignore` to
  zapewnia) i z prawem odczytu tylko dla użytkownika usługi.
- **To, co czyta asystent, widzi jego dostawca.** Treść poczty i faktur zwrócona przez
  narzędzie trafia do Anthropic albo OpenAI jako część rozmowy. Zanim podłączysz skrzynkę,
  zdecyduj, czy to jest dopuszczalne dla Twoich danych.
- **Wstrzykiwanie poleceń (prompt injection).** E-mail albo faktura mogą zawierać tekst
  napisany po to, żeby zmanipulować asystenta. Ten serwer nie potrafi nigdzie pisać, więc taki
  tekst nie zmusi go do działania, ale wciąż może zafałszować podsumowanie. Traktuj
  odczyt niezaufanej poczty przez asystenta z odpowiednią ostrożnością.
- **Logi** zawierają nazwy narzędzi, wywołującego i czasy, bez argumentów i wyników narzędzi.
  Nieoczekiwany błąd jest logowany razem ze śladem stosu.
- **Limit żądań.** Przykład dla nginx zawiera limit żądań. Konfiguracja z Caddy go nie ma.
- Historia repozytorium jest skanowana pod kątem sekretów narzędziem gitleaks w CI.

Zgłaszanie podatności: [SECURITY.md](SECURITY.md).

## Potrzebujesz innego systemu?

Dodanie systemu to napisanie jednego modułu;
[docs/dodawanie-modulu.md](docs/dodawanie-modulu.md) pokazuje to na przykładzie modułu
Fakturowni.

Dorabiamy też integracje na zamówienie, na przykład wFirma, inFakt, iFirma albo wyciągi
bankowe. Kontakt: biuro@infortel.pl

## Praca nad kodem

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

## Licencja

[MIT](LICENSE)
