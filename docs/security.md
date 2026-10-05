# Security boundaries

## Identity and authorization

Every resource read, search, raw download, evidence lookup and proposal write is scoped to a server-derived workspace. Client-supplied workspace IDs do not grant access. Roles are reader, researcher, editor and admin. Researchers can acquire/read/propose; editors publish; administrative source schedules and pipeline configuration require admin. Password accounts store salted scrypt hashes, sessions store only token hashes and can be revoked. Login throttling is persisted.

Static bearer credentials are supported for service clients via API_TOKENS_JSON. Map each agent to a researcher role rather than using the admin convenience API_TOKEN. Session cookies are HttpOnly/SameSite and the Next proxy rejects cross-site writes. Normal account mode never injects a fallback admin token. Demo and single-user modes must be explicitly enabled and remain loopback-only.

A public production frontend requires TLS, trusted reverse-proxy configuration, access logging policy, unique infrastructure credentials, and operational review. Compose is a local development deployment, not a hardened internet-facing reference architecture.

## Source network policy

Unknown sources use a bounded direct public HTTP client. It rejects credentials, non-HTTP(S) schemes, non-web ports, local/reserved/private IPv4/IPv6, ambiguous control characters and unsafe redirects. DNS is bounded and revalidated; the TCP connection pins a vetted address and TLS verifies the original hostname. Proxy environment variables and source cookies are not inherited. All resolved addresses must be public. This conservative policy may reject legitimate services; it does not silently bypass access restrictions.

Imported original URLs are declared provenance, not network requests. They receive syntax validation without DNS lookup. Metadata explicitly distinguishes user-uploaded saved bytes from application-fetched content.

Dynamic browsing goes through the optional bundled Playwright renderer and DNS-pinning forward proxy in deploy/renderer, accessed through a configured HTTPS endpoint and explicit isolated-egress deployment contract. An environment variable cannot itself enforce network isolation: the operator must enforce it in the renderer's network namespace/proxy. The API process never launches an unrestricted browser.

## Files and display

The parser consumes stored bytes. HTML and PDF parsing runs in a separate resource-bounded subprocess with offline model configuration and credentials removed. CPU/memory/wall/output limits reduce, but do not eliminate, parser risk. Deployment containers should still be unprivileged, read-only except designated data volumes, and egress-restricted.

Saved HTML is not served as executable HTML at the app origin. Raw files download as attachments with nosniff/sandbox headers. The reader uses text rendering. Content-addressed blobs are verified before publication/export/citation checks. Immutable ORM resources also have database mutation triggers; this is defense-in-depth, not protection from a malicious database administrator.

## Model/data policy

Source documents are untrusted data; they never define system instructions, grant tools or change schedule configuration. MCP excludes publish/admin/shell/credential tools. External model access needs an explicit provider gate, reviewed evidence policy and allowed classifications. The safe default source classification is internal; default allowed external classification is public. Sensitive transmission needs an additional explicit operator gate.

Regex contact redaction is only a convenience, not full DLP. No automatic classifier can establish that confidential source content is safe to send. Prompts contain minimized evidence IDs/quotes, not credentials or private URL query strings. Provider actual output and usage are retained for audit; private model reasoning is neither required nor displayed.

Durable paid-call reservations prevent retries from silently multiplying run spend. An accepted remote call whose response was lost remains an uncertain charge; the application does not claim it can undo that charge. Price-based reservations/estimates are not a provider's final invoice.

## Responsible operation

Apply retention and backup policies to database, object storage and Temporal data. Restrict backup access: these contain private research material. Rotate/revoke credentials through operator-controlled processes. Never put credentials or third-party full-text corpora in the public repository. Report suspected vulnerabilities privately to the repository owner; no security contact address is invented here.
