# Adding a module for another system

A module connects one external system (wFirma, inFakt, iFirma, a bank, ...) to the server.
This guide follows the Fakturownia module, which is the smallest complete example:

```
src/firma_mcp/modules/fakturownia/
  client.py     talks to the system's API and returns plain dicts
  __init__.py   reads configuration and registers the tools
tests/modules/test_fakturownia.py
```

Polish version: [dodawanie-modulu.md](dodawanie-modulu.md)

## The contract

A module is a `Module` object (see `src/firma_mcp/modules/__init__.py`) with:

| Field | Meaning |
| --- | --- |
| `name` | Short identifier, also the prefix of its tools |
| `status` | `PRODUCTION` or `EXPERIMENTAL`. Be honest: production means it runs against the real system |
| `summary` | One sentence added to the instructions the assistant sees |
| `load(env)` | Reads environment variables and returns a config, or `None` when the module is not configured |
| `register(mcp, config, ctx)` | Adds the tools |
| `requires` | Optional names of modules this one depends on |

The server enables a module exactly when `load` returns a config.

## Step 1: the client

Write a class that calls the API and returns plain dictionaries. Keep it free of anything
MCP-specific so it can be tested on its own. Rules the existing clients follow:

- **Read only.** Send only requests that cannot change data. The Fakturownia client sends
  only `GET`, and a test fails if another HTTP method appears in the file.
- **Raise `SourceError` for every failure**: network errors, HTTP errors, unexpected response
  shapes. Nothing else should escape.
- **Bound everything**: a timeout, a page limit, a size limit for binary content.
- **Never put tool input into a URL unchecked.** `_numeric_id` is the example.
- **Normalise money to decimal strings** such as `"123.45"`, as the other modules do.
- Accept an `httpx` transport in the constructor so tests can stub HTTP.

```python
class FakturowniaClient:
    def __init__(self, domain, api_token, *, transport=None):
        self._http = httpx.Client(
            base_url=base_url(domain), transport=transport, headers={"Authorization": f"Bearer {api_token}"}
        )

    def sales(self, days, unpaid_only=False) -> list[dict]: ...
```

## Step 2: configuration

`load` receives the environment and decides whether the module is on. `read_group` implements
the rule used everywhere: all variables set means on, none set means off, some set is an
error that stops the server at start-up.

```python
def load(env):
    values = read_group(env, "fakturownia", ("FAKTUROWNIA_DOMAIN", "FAKTUROWNIA_API_TOKEN"))
    if values is None:
        return None
    return FakturowniaConfig(domain=values["FAKTUROWNIA_DOMAIN"], api_token=values["FAKTUROWNIA_API_TOKEN"])
```

Validate what you can here (a malformed domain, an unknown environment name) and raise
`ConfigError`. A wrong setting should be reported at start-up, not on the first tool call.

Add every new variable to `.env.example` with a note on where its value comes from.

The real Fakturownia module adds one more condition on top of this example: because it is
experimental, it also requires `FAKTUROWNIA_ENABLED=true`. Consider the same for any module
that has not run against a live account yet.

## Step 3: the tools

`register` creates the client and declares the tools. Three things matter:

```python
def register(mcp, config, ctx):
    client = ctx.overrides.get("fakturownia") or FakturowniaClient(config.domain, config.api_token)
    ctx.services["fakturownia"] = client

    @mcp.tool(description="Sales invoices issued in the last `days` days ...", annotations=READ_ONLY)
    def fakturownia_sales(days: int = 7, unpaid_only: bool = False) -> dict:
        d = clamp_days(days, default=7)
        return ctx.run("fakturownia_sales", lambda: listing(client.sales(d, unpaid_only)))
```

1. **`ctx.run(name, fn)`** wraps every call. It checks that the caller is allowed, logs the
   call without its arguments, and turns a `SourceError` into `{"error": ..., "source": ...}`
   so that one failing system does not break the others.
2. **`annotations=READ_ONLY`** tells Claude and ChatGPT that the tool does not change anything.
3. **`ctx.overrides`** lets tests pass a fake client. **`ctx.services`** publishes your client
   to modules registered after yours.

Write the description for the assistant: say what the tool returns, what the parameters mean
and their limits, and when to use it. Prefix tool names with the module name.

## Step 4: register the module

Export a `MODULE` and add it to `all_modules()` in `src/firma_mcp/modules/__init__.py`:

```python
MODULE = Module(name="fakturownia", status=EXPERIMENTAL, summary="...", load=load, register=register)
```

The order in `all_modules()` is the registration order. A module that uses another one's
client must come after it. A tool that needs two systems is its own small module with
`requires`; `booking.py` (KSeF plus Merit) is the example.

## Step 5: tests

Test against stubbed HTTP responses with `httpx.MockTransport`; see
`tests/modules/test_fakturownia.py`. Cover at least:

- the request you send (path, parameters, authentication header, nothing secret in the URL),
- the mapping of a documented response into your output shape,
- paging and its limit,
- each failure mode becoming a `SourceError`,
- `load` for the configured, unconfigured and partly configured cases,
- that the client cannot write.

Use made-up data only. Never paste a real response from your own account into a test.

Stubs prove that the code matches the documentation, not that it matches the real system.
Until the module has run against a live account, mark it `EXPERIMENTAL` and say so in the
README.

## Checklist

- [ ] Client is read-only, bounded, and raises only `SourceError`
- [ ] `load` returns `None` when unconfigured and raises `ConfigError` when half-configured
- [ ] Every tool goes through `ctx.run` and has `annotations=READ_ONLY`
- [ ] Variables documented in `.env.example`
- [ ] Module added to `all_modules()` and to the status table in both READMEs
- [ ] Tests use only made-up data; `pytest` and `ruff check .` pass
