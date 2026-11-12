# firma-mcp

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI
assistant look into a small Polish company's back office: invoices in KSeF, bookkeeping in
Merit Aktiva, the company mailbox, and (experimentally) Fakturownia.

You run it on your own server, give it your own credentials, and add it to Claude or ChatGPT
as a remote connector. One installation serves one company.

Polish version: [README.pl.md](README.pl.md)

## What it is for

It was built for a daily "morning brief": an assistant reads the new mail, the invoices that
arrived in KSeF, and what is booked and paid in the accounting system, then reports what
needs attention. Every tool only reads. Nothing here can send a message, issue an invoice,
or change or delete anything in the connected systems.

## Supported systems

| System | Module | Status | What that means |
| --- | --- | --- | --- |
| KSeF 2.0 (Krajowy System e-Faktur) | `ksef` | Production | Runs daily against the production KSeF API in the author's company. |
| Merit Aktiva / 360 Księgowość | `merit` | Production | Runs daily against the Polish endpoint. The Estonian and Finnish endpoints are untested. |
| Mail (IMAP) | `mail` | Production | Runs daily against one IMAP server (TLS, port 993, app passwords). Other servers are untested. |
| Fakturownia | `fakturownia` | **Experimental** | Written from the public API documentation. Tested only against stubbed responses, never on a live account. |

"Production" describes one company's installation. It is not a claim about anyone else's.

A module switches on only when all of its settings are present. The rest of the server works
without it.

## Tools

| Tool | Module | What it returns |
| --- | --- | --- |
| `mail_list` | mail | Recent messages in a mailbox: sender, subject, date, preview, attachment names |
| `mail_get` | mail | Full text of one message |
| `mail_attachment` | mail | Text of a PDF, DOCX or TXT attachment, or raw bytes as base64 |
| `merit_purchases` | merit | Booked purchase invoices, optionally only unpaid |
| `merit_sales` | merit | Issued sales invoices, optionally only unpaid |
| `merit_invoice` | merit | Details of one invoice, optionally with its PDF |
| `merit_payments` | merit | Recorded incoming and outgoing payments |
| `payment_check` | merit | Whether a payment of a given amount has already been made |
| `ksef_received` | ksef | Invoices received in KSeF; with Merit enabled, also whether each is already booked |
| `ksef_invoice` | ksef | XML of one KSeF invoice |
| `booking_suggestions` | ksef + merit | For unbooked KSeF invoices: the ledger account and VAT scheme used before for the same seller |
| `fakturownia_sales` | fakturownia | Sales invoices (experimental) |
| `fakturownia_purchases` | fakturownia | Cost invoices (experimental) |
| `fakturownia_invoice` | fakturownia | Details of one invoice, optionally with its PDF (experimental) |
| `fakturownia_payment_status` | fakturownia | Paid, remaining and overdue state of one invoice (experimental) |

All tools carry the MCP annotation `readOnlyHint: true`. Mail is opened with IMAP `EXAMINE`
and fetched without marking anything as read.

Tool descriptions and error messages are in English. A few returned values are in Polish,
because they describe Polish bookkeeping and existing clients rely on them: payment
directions (`wpływ`, `wypływ`, `kompensata`, `nieznany`) and the note for a seller without
history (`nowy kontrahent — brak historii`). Data from the connected systems is passed
through in whatever language it was written.

## Run it with Docker Compose

You need a machine with Docker, a domain name pointing at it, and ports 80 and 443 open.

```bash
git clone https://github.com/zacharm-ewsoft/firma-mcp.git
cd firma-mcp
cp .env.example .env
chmod 600 .env
# edit .env: see "Configuration" below
docker compose up -d
docker compose logs -f firma-mcp
```

The Compose file starts two containers: the server, and [Caddy](https://caddyserver.com),
which obtains and renews a Let's Encrypt certificate for `FIRMA_DOMAIN`. The server is not
published to the host; only Caddy is.

On a correct start the log shows which modules are enabled:

```
INFO firma_mcp module enabled: ksef (production)
INFO firma_mcp starting: auth=github modules=ksef url=https://mcp.example.com/mcp
```

If the configuration is incomplete the server prints what is missing and exits with code 2.

Prefer no Docker? `deploy/` has an example nginx site and systemd unit. That is how the
author's own installation runs.

## Configuration

Everything is set through environment variables. [.env.example](.env.example) lists every
variable with a note on where to get its value. In short:

1. **Server address**: `FIRMA_BASE_URL` (for example `https://mcp.example.com`) and
   `FIRMA_DOMAIN` (the same host, used by Caddy).
2. **Authentication** (required, see below).
3. **Modules**: fill in the sections for the systems you use and leave the others empty.

### Authentication

There is no unauthenticated mode. Without valid authentication settings the server refuses
to start.

**GitHub sign-in (default, needed for claude.ai and ChatGPT).** Both assistants connect to
remote MCP servers with OAuth, so the server needs an identity provider. It uses a GitHub
OAuth app that you create yourself:

1. On GitHub open **Settings > Developer settings > OAuth Apps > New OAuth App**.
2. Set **Homepage URL** to your `FIRMA_BASE_URL` and **Authorization callback URL** to
   `<FIRMA_BASE_URL>/auth/callback`.
3. Copy the client ID and a new client secret into `GITHUB_CLIENT_ID` and
   `GITHUB_CLIENT_SECRET`.
4. Put your GitHub user name in `FIRMA_ALLOWED_GITHUB_LOGINS` (several names are separated
   with commas). Anybody with a GitHub account can complete the sign-in; this allowlist,
   checked on every tool call, is what limits the server to you. An account that is not
   listed is refused, and the server does not start with an empty list. The sign-in asks
   GitHub only for the `read:user` scope.
5. Generate `FIRMA_JWT_SIGNING_KEY` with `openssl rand -hex 32`.

**Shared token (`FIRMA_AUTH_MODE=token`), optional.** One long secret sent as
`Authorization: Bearer <token>`. It is meant for testing and for your own scripts, not for
the assistants: connect Claude and ChatGPT with the GitHub sign-in above. ChatGPT cannot use
a token at all, and claude.ai supports request headers only as a limited beta. The server
rejects a token shorter than 32 characters at start-up.

## Connect to Claude

Steps follow Anthropic's documentation
([Add a connector that isn't in the directory](https://claude.com/docs/connectors/custom/add-unlisted)),
read in November 2026.

1. In Claude open **Customize > Connectors**. On Team and Enterprise plans an Owner adds the
   connector under **Organization settings > Connectors** first.
2. Select **Add**, then **Add custom connector**.
3. Enter a name and the URL `https://<your domain>/mcp`, then **Continue**.
4. Keep the authentication settings Claude marks as **Detected**, and click **Add**.
5. Click **Connect** and sign in with the GitHub account you put on the allowlist.
6. In a conversation, use the **+** button and **Connectors** to turn the connector on.

Claude Code can also connect with the shared token (for testing):

```bash
claude mcp add --transport http firma https://<your domain>/mcp \
  --header "Authorization: Bearer <FIRMA_AUTH_TOKEN>"
```

## Connect to ChatGPT

Steps follow OpenAI's documentation
([Connect and test your plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt)),
read in November 2026. Your plan and workspace settings decide whether you may add custom
MCP servers.

1. Open [chatgpt.com/plugins](https://chatgpt.com/plugins).
2. Select the plus button, then **Add custom MCP server**.
3. Enter a name and a description.
4. Under **Connection** enter `https://<your domain>/mcp`.
5. Configure authentication (OAuth), review the risk warning and select
   **I understand and want to continue**.
6. Select **Create as a plugin**, review the discovered tools, and sign in with GitHub when
   asked.
7. In a new conversation type `@` and pick the plugin.

OpenAI's documentation states that custom MCP servers do not need `search` and `fetch`
tools, so this server does not add them. They are only required for a server to act as a
"company knowledge" source.

## What has actually been tested

> **Current state.** This code was reorganised from a private installation into the modular
> form you see here. Since that reorganisation it has **not yet** been run against the live
> systems (KSeF, Merit, a real mailbox) and has **not yet** been connected to Claude or to
> ChatGPT. Everything below was verified with automated tests and local runs only. This
> notice will be updated after live testing.

Stated separately on purpose.

**Automated tests (run in CI on every push).** 229 unit tests. KSeF, Merit and Fakturownia
are tested against stubbed HTTP responses and mail against a fake IMAP mailbox. All test data
is made up.

**MCP Inspector.** The official [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector)
(version 2.15.0, CLI mode) was run against a local instance in token mode: it listed all 15
tools with their read-only annotations and called a tool successfully. Without a token it
was refused.

**OAuth surface, locally.** A local instance in GitHub mode was probed with plain HTTP
requests. An unauthenticated request gets `401` with a `WWW-Authenticate` header pointing at
the protected resource metadata. The authorization server metadata advertises PKCE `S256`,
dynamic client registration and client ID metadata documents. Registration succeeds for the
redirect URIs of Claude (`https://claude.ai/api/mcp/auth_callback`) and ChatGPT
(`https://chatgpt.com/connector_platform_oauth_redirect`, `https://chatgpt.com/connector/oauth/...`)
and is rejected for other hosts. This used a dummy GitHub client, so the round trip through
GitHub itself was not part of this test.

**Claude.** The private installation this project was extracted from is used every day as a
custom connector in claude.ai, with the same GitHub sign-in component and the same KSeF,
Merit and mail logic. The reorganised code in this repository has not itself been connected
to claude.ai yet.

**ChatGPT.** Not tested with a live ChatGPT account. Support rests on OpenAI's documented
requirements (Streamable HTTP at `/mcp`, OAuth 2.1 with PKCE and client registration,
read-only annotations) and on the local OAuth test above.

**Docker Compose.** The image builds and the two-container stack was started locally:
Caddy served HTTPS with a local certificate and proxied to the server. Obtaining a real
Let's Encrypt certificate on a public domain was not tested.

**Not tested.** Fakturownia against a live account. Merit outside Poland. IMAP servers other
than the one in use. Running with many mailboxes or very large accounts.

## Security notes

- **Read-only by construction.** The Merit facade exposes only read calls, the Fakturownia
  client only sends `GET`, and tests fail if a write call is added to either. The upstream
  credentials themselves may still allow writing, so store them carefully and prefer
  restricted credentials where the system offers them (KSeF does: grant the token only the
  permission to browse invoices).
- **Authentication is mandatory** and the server fails closed on incomplete configuration.
- **Secrets live in `.env` only.** Keep the file out of version control (`.gitignore` does
  this) and readable only by the service user.
- **What the assistant reads, its provider sees.** Mail and invoice contents returned by a
  tool are sent to Anthropic or OpenAI as part of the conversation. Decide whether that is
  acceptable for your data before connecting a mailbox.
- **Prompt injection.** An e-mail or an invoice can contain text written to manipulate an
  assistant. Because this server cannot write anywhere, such text cannot make it act, but it
  can still mislead a summary. Treat the assistant's reading of untrusted mail accordingly.
- **Logs** contain tool names, the caller and timings, not tool arguments or results. An
  unexpected error is logged with its traceback.
- **Rate limiting.** The nginx example includes a request limit. The Caddy setup does not.
- The repository's history is scanned for secrets with gitleaks in CI.

To report a vulnerability see [SECURITY.md](SECURITY.md).

## Need a different system?

Adding a system means writing one module; [docs/adding-a-module.md](docs/adding-a-module.md)
walks through it using the Fakturownia module as the example.

We also build integrations to order, for example wFirma, inFakt, iFirma or a bank feed.
Contact: biuro@infortel.pl

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

Layout:

```
src/firma_mcp/
  server.py      builds the server from the enabled modules
  config.py      core settings (address, authentication)
  auth.py        GitHub OAuth and shared-token modes
  toolkit.py     the wrapper every tool call goes through
  modules/       one package per external system
```

## Licence

[MIT](LICENSE)
