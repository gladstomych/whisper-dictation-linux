# whisper-dictation vocabulary agent

An instruction prompt for a coding agent (e.g. Claude Code) to build and refresh
a **custom-vocabulary bias sheet** for whisper-dictation, tuned to *this* machine.
Run it on each laptop (personal, work); it adapts to whatever sources exist.

> Why an agent, not a script: the hard part is judging which proper nouns matter
> and which are worth including — that's language, not regex. The agent extracts
> and ranks; **you** approve.

---

## Output contract

- Write approved terms to `$WHISPER_VOCAB_FILE`
  (default `~/.config/whisper-dictation/vocab.txt`).
- Format: one term or phrase per line; `#` comments and blank lines ignored.
- **Budget: ~100–120 terms, ~200 tokens total.** This is a hard ceiling — the
  Whisper/OpenAI prompt window is ~224 tokens and a long list dilutes biasing.
  Curate ruthlessly; more is worse.
- Preserve any existing `# pinned` block verbatim (hand-curated, never auto-edit).
  Regenerate only the `# auto (generated <date>)` block below it.

Target shape:

```
# pinned — hand-curated, the agent never touches this block
Sunny Chau
whisper-dictation

# auto (generated 2026-07-18) — regenerated on each run
Anthropic
kubectl
faster-whisper
...
```

---

## Hard rules (safety)

1. **Nothing leaves the machine during generation.** Read local files only. Do
   NOT send source contents (repos, transcripts, emails) to any external API.
   Only the *final term list* is ever sent — and only later, by whisper-dictation,
   at transcribe time.
2. **The sheet is sent to OpenAI on every cloud request.** On the cloud backend
   you're already sending your speech, so the terms add little — but the sheet
   goes out *every* request regardless of what you dictate, so still don't stuff
   genuine secrets in it (a codename you'd never say aloud to the API anyway).
   On the local backend nothing leaves at all.
3. **Never include secrets** — API keys, tokens, passwords, private hostnames,
   internal URLs, file paths, emails, phone numbers.
4. **Ask before touching sensitive sources** (email, work docs). Don't read them
   unprompted.
5. Idempotent: re-running preserves the `# pinned` block; report what changed
   (added / removed).

---

## Procedure

### 0. Scope
Ask the user:
- **Which profile** — personal or work? (Sets which sources to scan.)
- Confirm the source roots to scan (repo directories, etc.).

### 1. Discover sources (Tier 1 first — cheap, high-signal, deterministic)
Prefer structured sources; they're almost pure proper nouns with little noise:
- **Package manifests** under repo roots: `pyproject.toml`, `package.json`,
  `Cargo.toml`, `go.mod`, `requirements*.txt` → dependency / tool names.
- **Git author names**: `git -C <repo> log --format='%an' | sort -u` → collaborators.
- **Repo + top-level directory names, README titles** → project / product names.
- **Contacts** (if the user offers a vCard export) → people's names.

Tier 2 (only if the user wants the extra lift; noisier, more sensitive):
- **Claude session histories** `~/.claude/projects/*/*.jsonl` → recurring
  capitalized multi-word terms, tool/repo/file names.
- **Work** (work profile only, with explicit consent): Fathom meeting summaries,
  email subjects — attendee names, product names.

### 2. Extract candidates
Use judgment, not just frequency. Keep: proper nouns, product/tool/library
names, people, project codenames, domain jargon, non-obvious spellings/acronyms.
Drop: common English words, programming keywords, numbers, hashes/UUIDs, paths,
anything that transcribes fine already. Dedup case-insensitively; prefer the
canonical casing (`kubectl`, `PyTorch`, `Anthropic`).

### 3. Rank & prune to budget
Score by frequency × distinctiveness (a term common in the user's world but rare
in general English scores high). Keep the top ~100–120. When in doubt, cut —
a tight sheet biases better than a bloated one.

### 4. Review with the user
Present the ranked candidates **grouped** — People / Projects & products /
Tech & tools / Other — and let the user:
- **drop** noise or anything they don't want in a sheet sent on every request,
- **add** hand-picked terms (these go in the `# pinned` block).
Iterate until they're happy. Do not write until approved.

### 5. Write
- Preserve/create the `# pinned` block (user's hand-adds live here).
- Replace the `# auto (generated <date>)` block with the approved terms.
- Stay under budget; if over, show what you'd cut and confirm.

### 6. Report
Summarize: N terms written, added/removed vs last run. Note: vocab is read at
transcribe time, so **no logout/reload needed** — the next dictation uses the
new sheet immediately.
