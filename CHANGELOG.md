# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Added an agent skill (`.claude/skills/mac-messages/SKILL.md`) that documents when and how to call each MCP tool, plus a README section on installing it for another project or agent.
- Added schema-faithful test databases: `tests/fixtures/chat_schema.sql` and
  `tests/fixtures/addressbook_schema.sql` are DDL dumped from a real Mac
  (macOS 27), and `tests/chatdb.py` loads them into temporary SQLite files that
  the code under test reads through its normal paths. `tests/test_real_schema.py`
  runs every read path's SQL against them, so a misspelled column or wrong join
  fails in CI rather than on a user's machine.
- Added `mac-messages-cli`, a terminal interface for the local Messages and Contacts databases: `recent`, `search`, `contact`, `contacts`, `chats`, `attachments`, `attachment`, `send`, and `check`. It reads and writes the same data as the MCP server under the same macOS permissions. `send` prompts for confirmation and refuses to send from a non-terminal stdin unless `--yes` is passed.
- Attachment metadata can be read without loading inline image bytes (`_describe_attachment`), which the CLI uses to print an attachment's path and optionally copy the file.

- Reads now render per-message context when `chat.db` carries it: the service
  (`[iMessage]`/`[SMS]`/`[RCS]`), inbound `[unread]`, outbound `[not delivered]`,
  `[tapback: ...]`, and `[reply]`. Tapback rows, which carry no body of their
  own, are no longer dropped, and a row from an older schema renders exactly as
  it did before.
- `tool_get_recent_messages` gained `limit`, `offset`, `start_date`, `end_date`,
  `unread_only`, and `since_rowid`, and reports the next `offset` when a page
  fills up. A `since_rowid` read ignores the `hours` window and returns the
  oldest message first.
- `tool_fuzzy_search_messages` gained `contact`, `chat_id`, `start_date`,
  `end_date`, and `limit`; ranked matches beyond `limit` are reported as clipped.
- `tool_list_conversations` lists 1:1, business, and group conversations with
  kind, message count, unread count, and last activity, which named group chats
  alone could not do.
- `tool_wait_for_new_messages` blocks until a message newer than a ROWID cursor
  exists, bounded to 300 seconds per call.
- `tool_search_attachment_contents` searches inside attachment contents. Text
  formats work out of the box; PDFs need the optional `pypdf` package and images
  need `pytesseract` plus the tesseract binary. Rows it cannot extract are
  reported instead of silently skipped.
- `tool_send_message` gained `attachment_paths` for iMessage file sends and
  `confirm=true`, which asks the human through MCP elicitation and sends nothing
  when the client does not implement elicitation.
- `tool_create_contact` creates one Contacts.app entry with a phone number.
- `tool_fuzzy_search_messages` consults a local full-text index first
  (`mac_messages_mcp/search_index.py`, SQLite FTS5 in a private file under
  `~/Library/Application Support/mac-messages-mcp/`; `MAC_MESSAGES_SEARCH_INDEX`
  overrides the path, `none` disables). The index is built from decoded message
  bodies once and then updated only for ROWIDs above the last one indexed, so a
  search over all history is fast and complete; the fuzzy pass still ranks the
  hits. Previously the search scanned only the newest few thousand candidate
  rows and, because modern macOS stores most bodies only in `attributedBody`
  where SQL cannot filter them, silently missed anything older.
  `tool_check_db_access` reports the index size.
- The group-chat name map and chat-identifier lookups are cached per database
  until the chat table's (count, max ROWID) fingerprint changes, so a page of
  messages or a scoped poll no longer re-reads the chat table; both spellings
  of a chat identifier hit the same cache entry. `tool_check_imessage_availability`
  asks whether one error-free iMessage row exists (`EXISTS … LIMIT 1`) instead
  of counting every message the handle ever carried.
- `tool_search_attachment_contents` searches extracted attachment text through
  the local index (`attachments_fts`) instead of opening and extracting every
  candidate file on each call. Text is extracted once per attachment,
  incrementally by ROWID; files not yet downloaded from iCloud are remembered
  and retried on later searches, and the report says how many are still
  awaiting download. Snippets come from the stored text. Extraction helpers
  moved to `extract.py` so both the search and the index can use them.
- `tool_list_scheduled_messages` also lists messages scheduled from
  Messages.app itself (Send Later, macOS 15+), read from `message.schedule_type`
  / `schedule_state`, as `apple-<ROWID>` entries with their send time and
  recipient; it says to cancel or edit those in Messages.app.
- More of what chat.db knows is shown. Attachments: a missing file is reported
  as `[not downloaded from iCloud]` (from `attachment.transfer_state`) rather
  than `[missing on disk]` when Messages never fetched it, and Genmoji carry
  their description (`genmoji: a cat wearing a top hat`). Messages: subject
  lines (`[subject: …]`), send effects (`[effect: impact]`), voice memos that
  were played (`[played]`), `[time sensitive]`, `[delivered quietly]`; JSON adds
  `subject`, `effect`, `played_at`, `time_sensitive`, `delivered_quietly`,
  `kt_verified` (Contact Key Verification), `via_account` and `to_my_address`
  (which of your own numbers or emails a message went through), and attachment
  `downloaded` / `genmoji`. Optional columns are probed per database.
- Message-semantics helpers (bodies, tags, tapbacks, replies, events, effects)
  moved from `read.py` into `semantics.py`; `read.py` keeps the queries and
  rendering.
- App balloons and edit history are decoded (`mac_messages_mcp/payloads.py`).
  A link preview shows its title (`[link preview: "How to brew better
  coffee"]`, JSON `link: {title, url}`); other iMessage-app balloons show
  their readable content (`[app: PeerPaymentMessagesExtension — $25.00; Dinner
  split]`, JSON `app_payload`); an edited message shows what it said before
  (`[edited; was: "see you at 7"]`, JSON `edit_history`). The blobs are fetched
  in one extra query only for the rows on the page that have a balloon or an
  edit, since link previews can carry images. The typedstream decoder moved to
  `typedstream.py` so both reads and payload decoding can use it.
- New `tool_get_conversation(chat_id, limit, before_rowid, after_rowid)`
  reads one thread in chronological order with cursors to page older or newer,
  headed by the chat's name, kind and participants. Messages that @-mention
  you are tagged `[mentions you]` (JSON `mentions_me`), and
  `tool_list_conversations` flags such threads `[mentions you]` (JSON
  `unseen_mentions`), from `message.has_unseen_mention`.
- Contact resolution follows Apple's own grouping of a person's handles,
  `handle.person_centric_id`. Reading or searching attachments by a phone
  number now also covers that person's iMessage email handles (and vice
  versa) without the AddressBook card having to list them, a handle the
  AddressBook does not know gets its name from a sibling handle it does know,
  and JSON senders carry `person_id`. Handles Apple has not linked are left
  separate.
- Reads surface the details chat.db records but used to drop or flatten:
  tapbacks quote what they reacted to (`[tapback: loved → "Dinner at…"]`,
  including custom-emoji tapbacks), replies quote the original
  (`[reply to: "…"]`), edited messages are tagged `[edited]`, unsent messages
  appear as `[unsent]: (unsent message)` instead of vanishing, audio messages
  and link previews are tagged, other iMessage-app balloons show `[app: …]`,
  group events render as `[event]: Alice named the group "Trip"` / `added Bob`
  / `left the group` instead of being skipped, and sent messages show
  `[read 09:12]` or `[delivered 09:20]` receipts. JSON output carries
  `tapback`, `reply_to`, `edited_at`, `unsent_at`, `is_audio`, `link_preview`,
  `app_bundle`, `event`, `delivered_at`, and `read_at`. Optional columns are
  probed once per database, so an older macOS without them still reads.
- Message lines say who an outbound message went to: `You → Alice` on a 1:1
  thread (the counterpart handle on the row), plain `You` in a group. Group
  chats without a name, which is most of them, are labelled by participants:
  `[group: Alice, Bob, Carol +2]`; named groups keep `[Family]`. JSON output
  carries `to` and a `chat` object (id, name, kind, participants).
- `tool_get_chats` lists every group chat, naming unnamed ones
  `(unnamed: Alice, Bob, Carol)`; it used to list only chats with a
  display name. New `tool_get_chat_participants(chat_id)` lists a chat's
  members with names and addresses.
- `format="json"` on `tool_get_recent_messages`, `tool_fuzzy_search_messages`,
  `tool_list_conversations`, `tool_search_attachments`, and
  `tool_wait_for_new_messages` returns one JSON object inside the usual
  untrusted fence: `{"messages": [...], "cursor": {"next_since_rowid": N},
  "page": {...}}` (or `conversations` / `attachments`), with one field per
  fact, bodies that round-trip exactly (newlines included), ISO-8601
  timestamps carrying the Mac's UTC offset, and every string neutralized
  through the same boundary as text output.
- `tool_schedule_message`, `tool_list_scheduled_messages`, and
  `tool_cancel_scheduled_message` manage a send queue that is persisted to
  `~/Library/Application Support/mac-messages-mcp/scheduled_messages.json`
  (private file, atomic writes; `MAC_MESSAGES_SCHEDULE_STORE` overrides the
  path, `none` disables). Pending jobs are reloaded when the server restarts: a
  job that came due while the server was down is sent if it is within an hour
  of its send time (and annotated as late), and marked `missed` otherwise; a
  job interrupted mid-send is marked `failed` rather than risking a duplicate.
- MCP prompts: `triage_unread_messages`, `summarize_recent_messages`, and
  `draft_reply`.

### Changed
- Scope resolution (`contact` / `chat_id` → which handles or chat a read
  matches) moved from `read.py` into `scope.py` and returns a `Scope` with
  named fields and a `sql_clauses()` method, so the three read tools share one
  filter builder instead of each rebuilding the `handle_id IN (...)` /
  `chat_message_join` clauses. The old five-tuple still unpacks. `session.py`
  gathers every process-level cache (sender names, chat lookups, column
  probes, SQLite connections, send budget, search index) behind
  `Session.reset()` / `Session.status()`; the test suite resets through it.
- `mac_messages_mcp.messages` re-exports only public names. It is generated by
  `scripts/gen_facade.py` (checked in; `--check` runs in the test suite), and
  private helpers are imported from the module that owns them. CI now
  type-checks the whole package rather than `server.py` alone, and the
  package is clean under that check.
- The README documents an always-on Mac setup (read-only or confirm-every-send
  profiles with a send cap, recipient allow-list, and attachment root), and
  lists `tool_get_conversation`. The stale `memory-bank/` design notes are
  removed from the repository and the sdist.
- `tool_list_conversations` no longer joins every message in the database. Last
  activity and counts come from `chat_message_join.message_date` (indexed),
  unread counts start from the small unread set, and the new "since last
  opened" count range-scans each chat from `chat.last_read_message_timestamp`.
  7.5x faster on a 20k-message fixture; the gap grows with history. It also
  hides archived threads by default (`include_archived=true` lists them with
  `[archived]`), tags Unknown Senders threads `[unknown sender]`, and reports
  how many inbound messages arrived since you last opened each thread, which is
  a steadier triage signal than Apple's per-message read flag. JSON output
  carries `archived`, `filtered`, `unseen_since_last_opened`, `last_opened_at`.
- Reads reuse one read-only SQLite connection per thread instead of opening a
  new one for every query, and resolve every sender on a page with one batched
  handle lookup (plus one chat-name fallback query) instead of one or two
  queries per rendered message; resolved names are cached per database until
  the AddressBook cache refreshes. A miss in the canonical-form handle lookup
  no longer re-reads and re-canonicalises the whole handle table every time.
  Measured on a 50-message page: 55 connections and 50 handle queries before,
  1 connection and 1 handle query after (`tests/test_performance.py`). The
  chat-name fallback for an unknown sender now ignores group chats, so an
  unknown number is no longer labelled with a group's name.
- `mac_messages_mcp.messages` is now a facade over eight focused modules:
  `applescript` (osascript), `db` (read-only chat.db access, Apple-epoch
  timestamps, the single date-range builder), `output` (format validation and
  JSON serialization), `contacts` (AddressBook, fuzzy matching, handle
  resolution, Contacts.app writes), `chats` (chat lookup, membership,
  conversation listing), `read` (body decoding, scope resolution, reads,
  waiting, fuzzy search), `send`, and `attachments`. Every name importable from `messages` before still is.
  `content.py` now imports from the owning modules instead of private names,
  attachment and content searches share `db.date_range_clauses`, and
  `read.list_named_chats` backs both `tool_get_chats` and the CLI.
  `tests/test_structure.py` pins these boundaries.
- Contact name resolution prefers an exact name or nickname match, so a query
  such as "Emma White" is no longer reported as ambiguous against look-alike
  names ("Jeremy White", "Alex Whitaker"). When the only remaining matches are
  one person's several handles (a phone number plus iMessage email addresses),
  reads and searches cover all of those handles and sends go to the phone
  number, instead of asking which handle to pick. Partial queries still return
  the full fuzzy list.
- Attachment paths are validated and resolved before any part of a message is
  sent, so a bad path cannot leave a partial send behind.
- Messages.app automation exposes no API for sending tapbacks, message effects,
  or subjects; creating or editing a group chat; editing, unsending, deleting,
  or marking a message read; or saving drafts. The server refuses those requests
  rather than faking them.

### Fixed
- Tools no longer block the server. FastMCP runs a plain function inline on
  its event loop, so `tool_wait_for_new_messages` (up to 300 s of polling), a
  first search over an unindexed database, or an AppleScript send stopped the
  server answering pings, cancellations, and other calls until it finished.
  Every synchronous tool and resource is now registered through a coroutine
  that runs it in a worker thread, and `tool_send_message` hands its
  AppleScript call to a thread too. The functions themselves stay ordinary
  synchronous calls for the Python API. The search index's shared SQLite
  connection, which only worked because everything ran on one thread, is now
  usable from any worker thread (all access was already serialised by a
  lock); before this fix a search from a second thread quietly fell back to
  the bounded LIKE scan.
- A JSON-format `tool_fuzzy_search_messages` call that the full-text index
  answered with no candidates raised `UnboundLocalError` instead of returning
  an empty result: the empty-JSON helper was defined below its first use.
  Found by type-checking the whole package.
- Date filters bind Apple-epoch timestamps as integers. They were bound as
  strings and compared with `CAST(m.date AS TEXT)`, which is lexicographic: a
  pre-2017 row storing seconds (nine digits) could sort above a nanosecond
  bound and appear in "the last 24 hours" (reproduced on the real schema in
  `tests/test_query_plans.py`). The integer comparison also lets SQLite seek
  `message_idx_date` instead of scanning it, which matters for date ranges in
  the past. The "overflow" rationale for the string bind was mistaken:
  Apple-ns values fit 64-bit integers with room to spare.
- `since_rowid` cursor reads order by `ROWID` instead of `date`. Filtering on
  `ROWID > ?` while ordering by `date` walked the date index from the oldest
  message on every poll; the primary-key seek is constant time. A cursor read
  is defined by ROWID order, so results are unchanged except when two rows
  disagree on date order.
- Message lines now show the message ROWID as `#N` after the timestamp, and a
  `since_rowid` read ends with a cursor note naming the highest ROWID seen.
  Previously no tool output ever exposed a ROWID, so the cursor that
  `since_rowid` and `tool_wait_for_new_messages` ask for could not be obtained
  from the tools themselves.
- A `since_rowid` read whose new rows all lack displayable text now reports the
  advanced cursor instead of "No new messages", so a polling caller does not
  re-read the same rows forever.
- Attachment placeholders are handled the way Messages writes them. macOS puts
  U+FFFC (object replacement character) into `attributedBody` where a file
  sits and omits it from `text`; the server looked for U+FFFD instead, so an
  attachment-only message rendered as a stray `￼` glyph rather than
  `[attachment]`, a text-plus-photo message carried the glyph in its body, and
  send verification could not match such a send's body and fell back to the
  newest row, sometimes reporting another send's outcome. Verified against a
  real macOS 27 blob, now kept (with its words substituted) in
  `tests/fixtures/attributed_body_sample.hex`.
- `start_date` / `end_date` filters are interpreted as days in the Mac's local
  timezone, matching the local timestamps the output shows. They were UTC days,
  so "messages from the 15th" missed the evening of the 15th anywhere behind
  UTC. All displayed times now go through one `db.local_timezone()` seam.
- `tool_list_conversations` labelled every one-to-one thread `[business]`,
  because it mapped `chat.style = 45` to business when 45 is the ordinary 1:1
  style. One-to-one threads are now `[direct]`; `[business]` is reserved for
  Messages for Business threads, recognised by their `urn:biz:` identifier.
- `tool_wait_for_new_messages` probes for new rows inside its `contact` or
  `chat_id` scope instead of the whole table. A message in another thread used
  to wake the wait and return "No new messages since ROWID N" immediately, and
  because the caller could not advance N, a polling loop spun without sleeping.
  An unresolvable scope is now reported at once rather than after the timeout.

### Security
- Server-side write policy, configured by environment and enforced inside the
  send and contact functions (so scheduled sends and direct callers are bound
  too), documented in `mac_messages_mcp/policy.py`:
  - `MAC_MESSAGES_ATTACHMENT_ROOTS` limits which folders files may be attached
    from. Default `~/Desktop:~/Downloads:~/Documents:~/Pictures`; paths are
    resolved through symlinks, and hidden files or anything under `~/Library`
    are always refused unless the value is `*`. Previously any file the user
    could read, `~/.ssh` keys or `chat.db` itself, could be sent to any number.
  - `MAC_MESSAGES_READ_ONLY=1` leaves the send, schedule, and contact-creation
    tools unregistered and makes the functions refuse.
  - `MAC_MESSAGES_REQUIRE_CONFIRM=1` makes `tool_send_message` ask the human
    through elicitation on every call regardless of its `confirm` argument,
    and refuses scheduled sends.
  - `MAC_MESSAGES_MAX_SENDS_PER_HOUR` caps send attempts in a rolling hour.
  - `MAC_MESSAGES_ALLOWED_RECIPIENTS` restricts sends to listed numbers (any
    spelling), emails, or chat IDs.
  `tool_check_db_access` reports the active policy.
- `tool_send_message` now routes its confirmation-path errors through the
  untrusted-output boundary like every other result.
- GitHub Actions workflows now declare read-only `GITHUB_TOKEN` permissions at the workflow level and grant write scopes only on the jobs that upload SARIF, submit SBOMs, comment on PRs, or publish releases.
- Contact/message text cleaning no longer uses a regex character class spanning U+24C2–U+1F251, which CodeQL flagged as an overly permissive range and which also stripped CJK.
- The MCPB builder fetches `uv` over HTTPS with an allowlisted redirect host set instead of `urllib.request.urlopen`.
- Added a bounded Atheris harness for phone/handle parsers (`fuzz/fuzz_phone.py`).

## [1.1.0] - 2026-09-01

### Security
- MCP tool and resource output derived from Messages and Contacts is serialized at a single untrusted-output boundary (`present_untrusted_output`). Newlines become the literal two-character sequence `\n`; invisible, bidi, and format Unicode is escaped; fence terminators inside content are defanged; and the payload is wrapped in `<untrusted-mcp-output>` so it is not treated as instructions. Fence idempotence uses an in-process marker, so attacker text that only looks already-fenced is still re-serialized. This is not an anti-injection guarantee.
- Clients must still gate `tool_send_message`. The locked `mcp` 1.3.0 FastMCP API has no elicitation, and a `confirm=True` argument is not human confirmation.
- Coordinated reports from Grégoire Compagnon (obeone) prompted this output-boundary work.
- CodeQL actions are pinned to commit SHAs and analyze Python with `security-extended` queries. Dependabot covers the `uv` and `github-actions` ecosystems. A Semgrep OSS scan (`semgrep scan` in the official `semgrep/semgrep` image) replaced the unused GitHub-starter Semgrep workflow that required `SEMGREP_APP_TOKEN`.

### Added
- Added `MAC_MESSAGES_REGION` to override the region used when parsing national-format numbers, for Macs configured for a different region than their phone numbers belong to.
- Added `phonenumbers` as a dependency, replacing the hand-rolled country-code heuristics.

### Fixed
- Phone numbers written in national format are now expanded to E.164 against the region the Mac is configured for instead of being assumed North American. A French number such as `06 39 98 00 01` was previously turned into `+10639980001`, which made contact listings show the wrong country code and made message lookups, attachment lookups and iMessage availability checks miss handles that exist in the database.
- Handle matching now compares numbers on their canonical E.164 form, so an E.164 input finds a handle stored in national format and the reverse. Lookups that find nothing through the indexed query fall back to a canonical scan of the handle table.
- `_looks_like_phone_input` accepts dot separators, so `05.39.98.00.03` is treated as a phone number rather than a contact name.
- Address book entries whose number cannot be read as a phone number, such as SMS short codes, entries carrying a trailing label, and foreign numbers saved without their `+`, keep a digits-only key instead of being dropped from the contacts map. Contact name resolution looks up the canonical form, the digits, and the national number, so both sides of the comparison meet whichever form the entry was stored under.
- The region is read from a macOS regional override when there is one. A Mac running in English with its Region set to France reports `en_US@rg=frzzzz`, whose effective region is France rather than the United States, and its national numbers were still being given a `+1`.
- Email handles are compared case-insensitively on both sides, so an address stored in mixed case is found whichever way it is typed.
- Email addresses are looked up as handles instead of being sent to fuzzy contact-name matching, which answered "No contacts found" for any address that is not in the address book and returned before the handle query could run.
- Recipients are refused for being dialable only from inside their own local area rather than for having fewer than ten digits. The digit floor rejected every numbering plan shorter than the North American one, an eight-digit Norwegian number among them, while still letting through the seven-digit North American local form it was meant to catch. Several numbering plans consider a short national form possible, a seven-digit North American local among them, so a half-typed number was being expanded and handed to Messages.app.
- Credit: Grégoire Compagnon (obeone) for the region-aware phone and email handle work ([#64](https://github.com/carterlasalle/mac_messages_mcp/pull/64)).

### Changed
- Upgraded Pillow from 12.2.0 to 12.3.0.
- Updated indirect dependencies: python-dotenv 1.2.2, idna 3.15, and pygments 2.20.0.
- GitHub Actions: `actions/checkout` v7.0.1 (SHA-pinned), `astral-sh/setup-uv` v10, and `pypa/gh-action-pypi-publish` 1.14.2.
- CI jobs now set `timeout-minutes` so a stuck macOS runner acquire fails closed.

## [1.0.0] - 2026-07-18

### Added
- Added production macOS CI across Python 3.10, 3.11, 3.12, and 3.13 with formatting, typing, distribution-build, clean-wheel-install, and MCPB architecture checks.
- Added structured GitHub issue forms and a pull-request template with validation and privacy prompts.
- Added complete setup guidance for Claude Desktop, Claude Code, Codex, Cursor, VS Code, and other stdio MCP clients.
- Added focused regression coverage for Messages and AddressBook database access failures.

### Changed
- Declared the project production-stable and established the first supported `1.x` compatibility contract.
- Replaced the interactive regex-based version script with an atomic, non-interactive UV-backed workflow that synchronizes `pyproject.toml`, `uv.lock`, and `manifest.json`.
- Made releases idempotent and reviewable: merged version changes are validated, published to PyPI through OIDC trusted publishing, annotated with a Git tag, and published as a GitHub release.
- Reworked the README around an install, permissions, client configuration, verification, privacy, and troubleshooting flow.

### Security
- Hardened local database access with SQLite read-only mode and `query_only` enforcement.
- Bounded and sanitized message output, escaped AppleScript inputs, and added execution timeouts around Messages automation.
- Kept attachment discovery metadata-first, with explicit fetches and size limits for inline images.
- Preserved tokenless PyPI publishing; no long-lived package index credential is stored in GitHub.

### Fixed
- Normalized direct phone recipients to reliable E.164 values and improved contact result formatting.
- Improved group-chat filtering, attachment handling, package installation verification, and permission diagnostics.

### Compatibility
- No intentional breaking Python or MCP tool API changes were introduced from 0.9.2; `1.0.0` marks the stable support boundary.

## [0.9.2] - 2026-05-10

### Added
- Added `chat_id` filtering to `tool_get_recent_messages` for group conversations returned by `tool_get_chats`.

### Fixed
- Normalized phone recipients to send-ready E.164-style values before dispatching to Messages.
- Made fuzzy contact lookup return send-ready phone numbers with a leading `+`.

## [0.9.1] - 2026-05-06

### Added
- Added `glama.json` metadata for Glama server ownership and profile completion.

### Changed
- Expanded MCP tool descriptions and parameter schemas with permissions, side-effect, return-shape, and tool-selection guidance.
- Updated Claude Desktop extension metadata to match the current package version.
- Modernized package license metadata to avoid setuptools deprecation warnings.

## [0.9.0] - 2026-05-06

### Added
- Added message attachment discovery, metadata search, and attachment fetching.
- Added Claude Desktop Extension packaging metadata.
- Added Dockerfile and packaging documentation for catalog and local inspection workflows.

### Fixed
- Hardened message output rendering for control characters, embedded newlines, and oversized message bodies.
- Resolved outstanding pull requests and issue-reported test coverage gaps.

## [0.7.0] - 2024-12-28

### 🚀 MAJOR FEATURE: SMS/RCS Fallback Support

This release adds automatic SMS/RCS fallback when recipients don't have iMessage, solving the "Not Delivered" problem for Android users and significantly improving message delivery reliability.

### Added
- **Automatic SMS/RCS Fallback**: Messages automatically fall back to SMS when iMessage is unavailable
- **iMessage Availability Checking**: New `tool_check_imessage_availability` MCP tool to check recipient capabilities
- **Enhanced Message Sending**: Improved AppleScript logic with built-in fallback detection
- **Clear Service Feedback**: Users are informed whether message was sent via iMessage or SMS
- **Android Compatibility**: Now works seamlessly with Android users and non-iMessage contacts

### Enhanced
- **Message Sending Logic**: Enhanced `_send_message_direct()` with automatic fallback
- **AppleScript Integration**: Improved error handling and service detection
- **User Experience**: Significantly reduced "Not Delivered" errors
- **Debugging Support**: Better visibility into delivery methods and failures

### New Functions
- `_check_imessage_availability()`: Check if recipient has iMessage available
- `_send_message_sms()`: Direct SMS sending function with proper error handling
- Enhanced fallback logic in existing message sending functions

### New MCP Tool
- `tool_check_imessage_availability`: Check recipient iMessage status with clear feedback
  - ✅ Shows iMessage available
  - 📱 Shows SMS fallback available
  - ❌ Shows when neither service is available

### Technical Implementation
- **Smart Detection**: Automatically detects phone numbers vs email addresses
- **Service Prioritization**: Tries iMessage first, falls back to SMS for phone numbers
- **Group Chat Handling**: Maintains iMessage-only for group chats (SMS doesn't support groups well)
- **Error Differentiation**: Distinguishes between iMessage and SMS delivery failures

### Testing
- Added `test_sms_fallback_functionality()` to integration test suite
- Validates new SMS functions don't crash with import errors
- Ensures proper exception handling for AppleScript operations
- Maintains backward compatibility with existing functionality

### Use Cases Solved
- **Android Users**: Messages now deliver automatically via SMS instead of failing
- **Mixed Contacts**: Seamless experience across iMessage and SMS contacts
- **Delivery Troubleshooting**: Can check iMessage availability before sending
- **Reduced Friction**: No manual intervention needed for cross-platform messaging

### Migration Notes
Users upgrading from 0.6.7 will immediately benefit from:
1. **Improved Delivery**: Messages to Android users work automatically
2. **Better Feedback**: Clear indication of delivery method used
3. **New Debugging**: Check iMessage availability proactively
4. **Fewer Errors**: Significantly reduced "Not Delivered" messages

This release makes Mac Messages MCP truly universal - working seamlessly with both iMessage and SMS/RCS recipients.

## [0.6.7] - 2024-12-19

### 🚨 CRITICAL FIXES
- **FIXED**: Added missing `from thefuzz import fuzz` import that caused fuzzy search to crash with NameError
- **FIXED**: Corrected timestamp conversion from seconds to nanoseconds for Apple's Core Data format
- **FIXED**: Added comprehensive input validation to prevent integer overflow crashes
- **FIXED**: Improved contact selection validation with better error messages

### Added
- Input validation for negative hours (now returns helpful error instead of processing)
- Maximum hours limit (87,600 hours / 10 years) to prevent integer overflow
- Comprehensive integration tests to catch runtime failures
- Better error messages for invalid contact selections
- Validation for fuzzy search thresholds (must be 0.0-1.0)
- Empty search term validation for fuzzy search

### Fixed
- **Message Retrieval**: Fixed timestamp calculation that was causing most time ranges to return no results
- **Fuzzy Search**: Fixed missing import that caused crashes when using fuzzy message search
- **Integer Overflow**: Fixed crashes when using very large hour values
- **Contact Selection**: Fixed misleading error messages for invalid contact IDs
- **Error Handling**: Standardized error message format across all functions

### Changed
- Timestamp calculation now uses nanoseconds instead of seconds (matches Apple's format)
- Error messages now consistently start with "Error:" for better user experience
- Contact selection validation is more robust and provides clearer guidance

### Technical Details
This release fixes catastrophic failures discovered through real-world testing:
- Message retrieval was returning 6 messages from a year of data due to incorrect timestamp format
- Fuzzy search was completely non-functional due to missing import
- Large hour values caused integer overflow crashes
- Invalid inputs were accepted then caused crashes instead of validation errors

### Breaking Changes
None - all changes are backward compatible while fixing broken functionality.

## [0.6.6] - 2024-12-18

### Issues Identified (Fixed in 0.6.7)
- Missing `thefuzz` import causing fuzzy search crashes
- Incorrect timestamp calculation causing poor message retrieval
- No input validation causing integer overflow crashes
- Inconsistent error handling and misleading error messages

## Previous Versions
[Previous changelog entries would go here]
