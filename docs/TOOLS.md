# Tool surface

Generated from the live MCP servers by `python -m scripts.generate_tools_doc`.
Do not edit by hand: CI regenerates this file and fails if it differs, so a
tool added without documentation breaks the build.

**18 tools across 3 first-party servers**, plus two third-party servers
used through the same discipline.

Last generated: 2026-11-15T11:05:11+00:00

---

## First-party servers

### `portfolio` (4 tools)

Read-only access to the freelancer's own evidence. No tool on this server changes anything, so it is safe to expose to any MCP host. Published separately so another freelancer can point Claude Desktop or Cursor at their own portfolio.

| Tool | Read/Write | Annotations | Auth | What it does |
| --- | --- | --- | --- | --- |
| `find_projects` | read | readOnly, idempotent | None (local, read-only) | Answer 'have I done X, for a client in industry Y?' exactly. |
| `get_case_study` | read | readOnly, idempotent | None (local, read-only) | Read one portfolio item in full, by its evidence_id. |
| `list_skills` | read | readOnly, idempotent | None (local, read-only) | List every skill the portfolio can actually evidence, with counts. |
| `search_evidence` | read | readOnly, idempotent | None (local, read-only) | Find portfolio evidence that proves ONE specific requirement. |

### `marketplace` (9 tools)

The only path to a marketplace. Wraps the marketplace port, so the same tools serve GigSim and live Upwork and the agent cannot tell which. Every write takes an idempotency key, supports `dry_run`, and refuses without a human approval token.

| Tool | Read/Write | Annotations | Auth | What it does |
| --- | --- | --- | --- | --- |
| `create_proposal_draft` | write | idempotent, openWorld | Bearer token to GigSim; OAuth 2.1 to live Upwork | Create a proposal DRAFT on the marketplace. Costs no Connects. |
| `get_client_history` | read | readOnly, idempotent, openWorld | Bearer token to GigSim; OAuth 2.1 to live Upwork | Read a client's hiring history: spend, hire rate, rating, past postings. |
| `get_connects_balance` | read | readOnly, idempotent, openWorld | Bearer token to GigSim; OAuth 2.1 to live Upwork | Check the remaining Connects balance and when it resets. |
| `get_job` | read | readOnly, idempotent, openWorld | Bearer token to GigSim; OAuth 2.1 to live Upwork | Read one job post in full, including its screening questions. |
| `get_market_rates` | read | readOnly, idempotent, openWorld | Bearer token to GigSim; OAuth 2.1 to live Upwork | Awarded-price statistics for a skill: p25, median, p75, fixed and hourly. |
| `list_messages` | read | readOnly, idempotent, openWorld | Bearer token to GigSim; OAuth 2.1 to live Upwork | List client messages, optionally for one thread. |
| `search_jobs` | read | readOnly, idempotent, openWorld | Bearer token to GigSim; OAuth 2.1 to live Upwork | Search open jobs on the marketplace. |
| `send_message` | write | idempotent, openWorld | Bearer token to GigSim; OAuth 2.1 to live Upwork | Send a message to a client. Requires human approval. |
| `submit_proposal` | write | **destructive**, idempotent, openWorld | Bearer token to GigSim; OAuth 2.1 to live Upwork | Submit a proposal draft. SPENDS CONNECTS AND CANNOT BE UNDONE. |

### `workspace` (5 tools)

Calendar and email. Holds are tentative and emails are drafts: nothing here commits the user to a meeting or sends mail on their behalf.

| Tool | Read/Write | Annotations | Auth | What it does |
| --- | --- | --- | --- | --- |
| `create_email_draft` | write | idempotent | None (mock); OAuth to Google when enabled | Create an email DRAFT. Never sends. |
| `create_tentative_hold` | write | idempotent | None (mock); OAuth to Google when enabled | Place a TENTATIVE hold on the calendar. Does not confirm a meeting. |
| `find_free_slots` | read | readOnly, idempotent | None (mock); OAuth to Google when enabled | Find genuinely free slots in the next N working days. |
| `list_holds` | read | readOnly, idempotent | None (mock); OAuth to Google when enabled | List every tentative hold currently on the calendar. |
| `release_hold` | write | idempotent | None (mock); OAuth to Google when enabled | Release a tentative hold that is no longer needed. |

---

## Third-party servers

| Server | Why it is here | Trust rationale |
| --- | --- | --- |
| **Upwork official MCP** (`https://mcp.upwork.com/mcp`) | Live mode: job search, proposal drafts, messages. | Third-party, operated by Upwork. Trusted because it is the platform's own server and the only sanctioned automation route; scraping is a terms violation. OAuth 2.1, tokens encrypted at rest, scoped per user. |
| **GitHub MCP** (`https://api.githubcopilot.com/mcp/`) | Ingests repository READMEs and code into the portfolio corpus. | Third-party, operated by GitHub. Trusted for repository ingestion because it is the official MCP server for a service the user already has an account with, and it is read-only in our configuration (repo contents only). |

Trust rationale for the first-party servers:

| Server | Rationale |
| --- | --- |
| `portfolio` | First-party. Reads only local, user-owned data. No network egress. |
| `marketplace` | First-party. Network egress to GigSim or the official Upwork MCP server only. |
| `workspace` | First-party. Local mock by default; Google Calendar/Gmail via their official APIs when enabled. |

---

## Per-agent tool allowlist

Each agent is handed only the tools it needs, filtered by name at the client.
This is a security boundary, not documentation: a node physically cannot call
what it was not given, and `tests/integration/test_graph_guarantees.py` proves
it by trying.

Note that the **Job Analyst holds no tools at all**. It is the node that reads
attacker-controlled text, and giving that node tool access is precisely the
combination that makes prompt injection dangerous.

| Agent | Server | Tools it may call |
| --- | --- | --- |
| `scout` | `marketplace` | `search_jobs`, `get_job` |
| `analyst` | — | **none** (pure reasoning; holds no tools) |
| `risk_assessor` | `marketplace` | `get_client_history` |
| `fit_matcher` | `portfolio` | `search_evidence`, `get_case_study`, `list_skills`, `find_projects` |
| `pricing` | `marketplace` | `get_market_rates`, `get_connects_balance`, `get_client_history` |
| `writer` | `portfolio` | `search_evidence`, `get_case_study` |
| `critic` | `portfolio` | `search_evidence`, `get_case_study` |
| `compliance` | — | **none** (pure reasoning; holds no tools) |
| `client_liaison` | `marketplace` | `list_messages`, `get_job`, `send_message` |
| `client_liaison` | `workspace` | `find_free_slots`, `create_tentative_hold`, `create_email_draft` |
| `submitter` | `marketplace` | `create_proposal_draft`, `submit_proposal` |

---

## Write-tool safety contract

Every write tool, on every server, satisfies all four of these. The contract
tests in `tests/contract/test_tool_schemas.py` enforce them.

| Property | Why | Enforced by |
| --- | --- | --- |
| `idempotency_key` is required | A retried call after a timeout must not submit twice or spend Connects twice. | `test_every_write_tool_takes_an_idempotency_key` |
| `dry_run` returns the exact payload and cost | The human approves the real call, not a summary of it. | `test_every_write_tool_supports_dry_run` |
| `approval_token` is required to execute | The agent cannot reach the marketplace on its own, whatever a job post tells it. | `test_every_write_tool_requires_an_approval_token` |
| Annotations are honest | `submit_proposal` spends Connects irreversibly and is marked `destructiveHint`. | `test_irreversible_tools_are_marked_destructive` |

## Schema snapshot

`tests/contract/tool_schema_snapshot.json` records every signature above. An
unintended change fails CI with a diff naming the tool. An intended change is
recorded with `make snapshot`, putting the new interface in the commit where a
reviewer sees it.
