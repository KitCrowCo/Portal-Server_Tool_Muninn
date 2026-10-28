# tools/muninn/huginn.py
"""
Huginn - the assistant that works through Muninn. It only proposes: a thread and/or a next step for a capture, move / delete / reduce for pad blocks, updated thread cards, and old captures that no longer matter (listed in Reduce).
Nothing changes until the person accepts; declined proposals and their replies are read back on the next pass, so it learns what to leave alone.
Configuration (data/muninn/huginn.json): the chat connection and model (the AI Manager's own fields, as in any pipeline node), how often it runs, and which logins have switched it on.
Per login: huginn_charter.md (what it may do, read at every pass - the person edits it), huginn_state.json (what it has already looked at), huginn_log.jsonl (every pass and what it produced).
Runs as one background task in the portal process, a pass every interval_min; Run now starts a pass at once. A pass handles one login at a time and one model call at a time.
Surveys (off until an admin sets a daily cap and the agent settings, and the login switches them on): for threads that went quiet, Huginn queues read-only agent runs through the agent module's "agent" service;
each run's report comes back to its thread as a capture, and every run is listed (by huginn) in Agent with its transcript, where it can be stopped.
"""
import re, json, asyncio
from pathlib import Path
from datetime import datetime, timedelta
from tools.muninn import muninn as MN

CFG_PATH = Path("./data/muninn/huginn.json")
_DEFAULTS = {"enabled": False, "conn_id": "", "model": "", "num_ctx": 16384, "temperature": 0.2, "interval_min": 30, "users": [], "max_pad_chars": 12000, "max_proposals": 4, "quiet_min": 10, "max_cards": 3, "max_card_chars": 8000, "survey_cap": 0, "stall_days": 7, "survey_gap_days": 14, "survey_users": [], "survey_settings": None, "moot_days": 30}
_TASK = None
_RUNNING = set()
_RUNS = set()
_FIELDS = None

CHARTER = """# Huginn charter
(Read by the assistant at the start of every pass. Edit it to change what it does.)

- Propose only. Never change a pad, a capture or a thread card directly; every change waits for my answer.
- Captures: suggest the thread a capture belongs to, and the next step it implies when it clearly implies one (with its date when it has one).
- Next steps with a date or a pin: say how much each matters (high / some / low) and why, in one line. My own ratings win.
- Pads are my scratch space. Move a block to a thread when it is a self-contained item (a task, an idea, a draft, a decision) that belongs with that thread.
- Move a block to the wiki when it is permanent information (setup and how-to, reference, records, story material): the page it belongs on from the list, or a new page next to related ones.
- Delete only exact duplicates, or text marked done or obsolete. When unsure, leave it.
- Reduce only long blocks that repeat themselves; keep every fact, name, number and date.
- Thread cards: fold new captures into the card; keep everything still true; never invent.
- A proposal I declined stays declined unless my reply says otherwise. Read my replies before proposing.
- At most a few proposals per pad per pass, the clearest first.
- Notes are temporary. Permanent information (records and official documents, setup and how-to, story material) belongs in its permanent place - the wiki, the knowledge base, a record - not in notes; a to-do that still matters becomes a next step. Point out old notes that no longer matter (the event passed, it was done, a later note replaced it); never ones that hold permanent information.
- Surveys (only when switched on): for a thread that went quiet, a careful read-only look by the agent that helps its next step - finding where something is, summarising what exists, listing open questions. Never a change.
- Everything stays on this server.
"""

# --- Config, charter, state, log ---

def config() -> dict: return {**_DEFAULTS, **(json.loads(CFG_PATH.read_text(encoding="utf-8")) if CFG_PATH.exists() else {})}
def save_config(**kw): CFG_PATH.write_text(json.dumps({**config(), **kw}, indent=1), encoding="utf-8")
def charter(user: str) -> str: return (MN._udir(user) / "huginn_charter.md").read_text(encoding="utf-8") if (MN._udir(user) / "huginn_charter.md").exists() else CHARTER
def save_charter(user: str, text: str): (MN._udir(user) / "huginn_charter.md").write_text(text, encoding="utf-8")
def _state(user: str) -> dict: return {"pads": {}, "triaged": [], "cards": {}, "judged": [], "surveys": [], "mooted": [], "places": [], **(json.loads((MN._udir(user) / "huginn_state.json").read_text(encoding="utf-8")) if (MN._udir(user) / "huginn_state.json").exists() else {})}
def _state_put(user: str, st: dict): (MN._udir(user) / "huginn_state.json").write_text(json.dumps(st, indent=1), encoding="utf-8")

def _state_patch(user: str, key: str, fn):
    """Replaces one entry of the state with fn(entry) and saves it."""
    st = _state(user)
    st[key] = fn(st[key])
    _state_put(user, st)
def log(user: str, event: str, **kw): MN._append(MN._udir(user) / "huginn_log.jsonl", {"t": MN._now(), "event": event, **kw})
def recent_log(user: str, n: int = 30) -> list: return MN._jsonl(MN._udir(user) / "huginn_log.jsonl")[-n:][::-1]
def running(user: str) -> bool: return user in _RUNNING

def fields():
    """Settings as a SettingsGroup. Connection, Model, context and temperature are the AI Manager's chat fields (the generate node's), so they list, label and warn as everywhere else;
    changing the connection re-renders the fields (intent muninn_huginn_refresh) so Model lists that connection's models. Built on first use - the AI Manager is loaded by then."""
    global _FIELDS
    if _FIELDS: return _FIELDS
    BI, AIM = MN.ENV["tools"]["built_ins"], MN.ENV["tools"]["ai_manager"]
    F = BI.SettingField
    _FIELDS = BI.SettingsGroup("huginn", "Huginn", [F("enabled", "Run passes on a schedule", "checkbox", default=False), *AIM.steps.chat_fields("muninn_huginn_refresh"), F("interval_min", "Every (minutes)", "number", default=30, step=1, min=5),
        F("quiet_min", "Pad / thread quiet before review (minutes)", "number", default=10, step=1, advanced=True, hint="Not reviewed while you are still typing in it."),
        F("max_proposals", "Proposals per pad per pass", "number", default=4, step=1, advanced=True), F("max_pad_chars", "Longest pad text sent (chars)", "number", default=12000, step=1, advanced=True),
        F("max_cards", "Card updates per pass", "number", default=3, step=1, advanced=True), F("max_card_chars", "Longest card + captures sent (chars)", "number", default=8000, step=1, advanced=True),
        F("survey_cap", "Agent surveys per login per day (0 = off)", "number", default=0, step=1, min=0, hint="Read-only agent runs for quiet threads, with the agent settings chosen below. Each login also switches them on for itself."),
        F("stall_days", "A thread is quiet after (days)", "number", default=7, step=1, min=1, advanced=True), F("survey_gap_days", "Survey the same thread again after (days)", "number", default=14, step=1, min=1, advanced=True),
        F("moot_days", "Point out notes that no longer matter, once older than (days; 0 = off)", "number", default=30, step=1, min=0, advanced=True, hint="They are listed in Reduce with the reason; nothing leaves until you destroy or seal it there.")], json_path=str(CFG_PATH))
    return _FIELDS

# --- Model calls ---

def describe() -> str:
    """Which connection and model a pass would use, for the settings view."""
    cfg = config()
    try: conn = MN.ENV["tools"]["ai_manager"].steps.resolve_chat_conn(cfg["conn_id"], "Huginn")
    except RuntimeError as e: return str(e)
    return f"""{conn.get("display_name", conn["_id"])} / {cfg["model"] or "auto (first non-embedding model)"}"""

async def _llm(system: str, prompt: str) -> str:
    cfg, AIM = config(), MN.ENV["tools"]["ai_manager"]
    conn, model = AIM.steps.resolve_chat(cfg["conn_id"], cfg["model"], "Huginn (Muninn > Huginn > Settings)")
    return "".join([t async for t, _ in AIM.connections.stream_llm(conn, [{"role": "system", "content": system}, {"role": "user", "content": prompt}], model, think=False, num_ctx=int(cfg["num_ctx"]), temperature=float(cfg["temperature"]))])

def _json_list(text: str) -> list:
    """The first JSON list in a reply (models often wrap it in prose or a code fence); [] when there is none."""
    m = re.search(r"\[.*\]", text, re.S)
    try: v = json.loads(m.group(0)) if m else []
    except json.JSONDecodeError: return []
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []

def _json_obj(text: str) -> dict:
    """The outermost JSON object in a reply; {} when there is none."""
    m = re.search(r"\{.*\}", text, re.S)
    try: v = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError: return {}
    return v if isinstance(v, dict) else {}

def _threads_text(user: str) -> str: return "\n".join(f"""- {t["slug"]}: {t["title"]}""" for t in MN.threads(user)) or "(none yet)"

# --- Triage: thread and next step for captures ---

_TRIAGE_SYS = "You sort a person's quick notes: which of their threads (topics) a note belongs to, and what next step it implies. You answer with a JSON list only, no other text."

def _triage_line(e: dict) -> str: return f"""[{e["id"]}] ({e["t"][:10]}; threads: {", ".join(e["threads"]) or "none"}; {"has a next step" if e["next"] else "no next step"}) {e["text"][:400]}"""

async def triage(user: str) -> int:
    """Up to 20 of the person's own captures not looked at yet that have no thread or no next step. A note is looked at once; a later pass does not re-ask about one it left alone. Captures carrying an @command are actions already, not notes."""
    st, asked = _state(user), {s["entry"] for s in MN.suggestions(user, "")}
    notes = [e for e in MN.entries(user) if e.get("author", "user") == "user" and not e.get("plain") and not e["commands"] and e["id"] not in asked and e["id"] not in st["triaged"] and not (e["threads"] and e["next"])][:20]
    if not notes: return 0
    reply = await _llm(_TRIAGE_SYS, f"""{charter(user)}\n\nToday is {datetime.now().strftime("%Y-%m-%d (%A)")}.\n\nThreads (slug: title):\n{_threads_text(user)}\n\nNotes - [id] (date captured; its threads; whether it already has a next step) text:\n{chr(10).join(_triage_line(e) for e in notes)}\n
Return a JSON list with one object per note that needs something: {{"entry": "<id in brackets>", "thread": "<slug or empty>", "next": "<next step or empty>", "reason": "<few words>"}}.
- thread: only for a note with no thread. Use an existing slug when one fits; name a new thread (short, lowercase, words joined by -) only when several notes share a clear topic no thread covers.
- next: only for a note with no next step that clearly implies something to do. Short and concrete, starting with a verb. When the note gives or implies a date, end with it as YYYY-MM-DD (work out "Friday" or "tomorrow" from the date captured).
Leave out notes that need neither - that is fine.""")
    by_id, made = {e["id"]: e for e in notes}, 0
    for r in _json_list(reply):
        e = by_id.get(r.get("entry"))
        if not e: continue
        thread = str(r.get("thread") or "").strip() if not e["threads"] else ""
        nxt = str(r.get("next") or "").strip()[:200] if not e["next"] else ""
        if thread or len(nxt) > 2: MN.suggest(user, e["id"], thread=thread, next_step=nxt if len(nxt) > 2 else "", reason=str(r.get("reason", ""))[:200]); made += 1
    st["triaged"] = (st["triaged"] + sorted(by_id))[-2000:]
    _state_put(user, st)
    log(user, "triage", notes=len(notes), suggestions=made, raw=reply[:2000] if not made else "")
    return made

# --- Signals: how much dated and pinned next steps matter ---

_SIGNAL_SYS = "You judge how much a person's next steps matter, so the important ones are shown first and the rest stay quiet. You answer with a JSON list only, no other text."

def _due_signals(user: str) -> list:
    """Open, not parked, pinned or dated within 14 days (or past), and not looked at yet. A step the person rated is never judged again."""
    st, sig, d0 = _state(user), MN.signals(user), datetime.now().date()
    out = [(e, i, n, MN.due(e, i, n)) for e, i, n in MN.open_next(user) if not MN.is_parked(e, i) and MN.skey(e, i) not in sig and MN.skey(e, i) not in st["judged"]]
    return [x for x in out if x[1] in x[0]["pinned"] or (x[3] and (x[3] - d0).days <= 14)][:15]

def _corrections(user: str) -> str:
    texts = {MN.skey(e, i): n for e in MN.entries(user) for i, n in enumerate(e["next"])}
    rows = [f"""- "{texts.get(r["key"], "?")[:120]}" -> {r["level"]}{": " + r["why"] if r["why"] else ""}""" for r in MN._jsonl(MN._udir(user) / "signals.jsonl") if r["author"] == "user"][-15:]
    return "\n".join(rows) or "(none yet)"

async def judge_signals(user: str) -> int:
    items = _due_signals(user)
    if not items: return 0
    rows = "\n".join(f"""[{MN.skey(e, i)}] (due {d.isoformat() if d else "no date - pinned"}; thread {", ".join(e["threads"]) or "none"}) {n}""" for e, i, n, d in items)
    reply = await _llm(_SIGNAL_SYS, f"""{charter(user)}\n\nToday is {datetime.now().strftime("%Y-%m-%d (%A)")}.\n\nHow the person rated earlier next steps (follow their sense of what matters):\n{_corrections(user)}\n\nNext steps:\n{rows}\n
Return a JSON list with one object per next step: {{"key": "<key in brackets>", "level": "high" | "some" | "low", "why": "<one short line: what happens if it is not done in time>", "actionable": true | false}}.
high = a real consequence soon (money, a person waiting, a deadline, health); some = it matters but can slip a little; low = nothing much happens if it waits. actionable = it can be done now (false when it waits on someone or something).""")
    keys, made = {MN.skey(e, i) for e, i, n, d in items}, 0
    for r in _json_list(reply):
        if r.get("key") in keys and r.get("level") in MN.LEVELS:
            MN.signal_set(user, r["key"], r["level"], why=str(r.get("why", "")), actionable=r["actionable"] if isinstance(r.get("actionable"), bool) else None, author="huginn"); made += 1
    _state_patch(user, "judged", lambda v: (v + sorted(keys))[-2000:])   # looked at once; one the model skipped is not asked again
    log(user, "signals", items=len(items), judged=made, raw=reply[:2000] if not made else "")
    return made

# --- Pads: move / delete / reduce / wiki proposals ---

_PAD_SYS = "You help a person reduce a scratch pad without losing anything they need. You propose changes to blocks of the pad; you never rewrite the whole pad. You answer with a JSON list only, no other text."

def _history_text(notes: list) -> str:
    rows = [f"""- note from {r["author"]}: {r["text"]}""" if r["kind"] == "note" else f"""- earlier proposal ({r["action"]}{" to " + r["target"] if r.get("target") else ""}) - {r["status"]}{": " + r["reply"] if r.get("reply") else ""}\n  block: {r["block"][:300]!r}""" for r in notes]
    return "\n".join(rows[-30:]) or "(none)"

def save_places(user: str, pages: list):
    """The wiki pages this login may add to, as last seen by the Muninn module (the background pass has no login to ask the wiki with)."""
    if _state(user)["places"] != pages: _state_patch(user, "places", lambda v: pages)

def _places_text(user: str) -> str: return "\n".join(_state(user)["places"][:400]) or "(no pages yet)"

def _valid(p: dict, text: str, notes: list, wiki: bool = False) -> bool:
    block, act, target = str(p.get("block", "")), p.get("action"), str(p.get("target", "")).strip()
    if act == "wiki" and not (wiki and target.endswith(".md") and ".." not in target.split("/") and not target.startswith("/")): return False
    if act not in ("move", "delete", "reduce", "wiki") or len(block.strip()) < 3 or text.count(block) != 1: return False
    if any(r["kind"] == "proposal" and r["block"] == block and r["status"] in ("pending", "declined") for r in notes): return False
    if act == "move" and not str(p.get("target", "")).strip(): return False
    return not (act == "reduce" and not (0 < len(str(p.get("replacement", "")).strip()) < len(block)))

async def review_pad(user: str, m: dict) -> int:
    cfg, text, notes, wiki = config(), MN.pad_text(user, m["id"]), MN.pad_notes(user, m["id"]), "wiki" in MN.ENV["services"]
    if not text.strip(): return 0
    places = f"""\n\nWiki pages (path) - for permanent information:\n{_places_text(user)}""" if wiki else ""
    reply = await _llm(_PAD_SYS, f"""{charter(user)}\n\nThreads (slug: title):\n{_threads_text(user)}{places}\n\nWhat has happened with this pad before (notes, earlier proposals and the person's answers - respect them):\n{_history_text(notes)}\n\nPad "{m["title"]}":\n<<<\n{text[:int(cfg["max_pad_chars"])]}\n>>>
\nReturn a JSON list of at most {cfg["max_proposals"]} proposals, clearest first: {{"action": "move" | "delete" | "reduce"{' | "wiki"' if wiki else ""}, "block": "<exact text copied from the pad, character for character, including line breaks>", "target": "<thread slug for move; optional for reduce{'; wiki page path for wiki (one from the list, or a new path ending .md next to related pages)' if wiki else ''}>", "replacement": "<shorter text, reduce only>", "reason": "<few words>"}}.
"block" must appear exactly once in the pad. Return [] when nothing should change.""")
    made = 0
    for p in _json_list(reply):
        if made < int(cfg["max_proposals"]) and _valid(p, text, notes, wiki):
            MN.pad_propose(user, m["id"], p["action"], p["block"], target=str(p.get("target", "")), replacement=str(p.get("replacement", "")), reason=str(p.get("reason", ""))[:200]); made += 1
    log(user, "pad_review", pad=m["id"], title=m["title"], proposals=made, raw=reply[:2000] if not made else "")
    return made

def _due_pads(user: str) -> list:
    """Pads changed since Huginn last read them, quiet for quiet_min minutes (not being typed in), with no proposal still waiting for an answer - open pads first, then the Pile."""
    st, quiet = _state(user), int(config()["quiet_min"]) * 60
    return sorted((m for m in MN.pads(user) if st["pads"].get(m["id"]) != m["version"] and (datetime.now() - datetime.fromisoformat(m["updated"])).total_seconds() > quiet and not any(r.get("status") == "pending" for r in MN.pad_notes(user, m["id"]))), key=lambda m: m["status"] != "open")

# --- Thread cards: updated card proposals ---

_CARD_SYS = "You keep a short card for one of a person's threads: where it stands, decisions, what was tried, the next small step. You update it from new notes without losing anything still true and without inventing. You answer with a JSON object only, no other text."

def _due_cards(user: str) -> list:
    """Threads with captures newer than both their card and Huginn's last look, quiet for quiet_min minutes, with no card proposal waiting."""
    st, quiet, waiting = _state(user), int(config()["quiet_min"]) * 60, {r["slug"] for r in MN.card_props(user)}
    out = []
    for t in MN.threads(user):
        base = max(MN.page(user, t["slug"])["meta"].get("updated", ""), st["cards"].get(t["slug"], ""))
        if t["last_capture"] > base and t["slug"] not in waiting and (datetime.now() - datetime.fromisoformat(t["last_capture"])).total_seconds() > quiet: out.append(t)
    return out[:int(config()["max_cards"])]

def _card_history(user: str, s: str) -> str: return "\n".join(f"""- {r["t"][:10]} {r["status"]}{": " + r["reply"] if r.get("reply") else ""}""" for r in MN.card_props(user, s, "")[-8:]) or "(none)"

async def refresh_card(user: str, t: dict) -> int:
    cfg, p = config(), MN.page(user, t["slug"])
    since = p["meta"].get("updated", "")
    new = [e for e in reversed(MN.entries(user, t["slug"])) if e["t"] > since]
    caps = "\n".join(f"""- {e["t"][:10]} ({e.get("author", "user")}): {e["text"]}""" for e in new)[-int(cfg["max_card_chars"]):]
    nexts = "\n".join(f"- {n}" for e, i, n in MN.open_next(user, t["slug"])) or "(none)"
    reply = await _llm(_CARD_SYS, f"""{charter(user)}\n\nThread "{p["title"]}" (#{t["slug"]}). Its card now:\n<<<\n{p["body"].strip()[:int(cfg["max_card_chars"])] or "(empty)"}\n>>>\n\nCaptures since the card was last written, oldest first:\n{caps}\n\nOpen next steps on this thread:\n{nexts}\n\nEarlier card proposals and the person's answers (respect them):\n{_card_history(user, t["slug"])}
\nReturn {{"body": "<the whole updated card, Markdown>", "reason": "<few words: what changed>"}}. Keep every fact, decision, name, number and date that is still true; fold the new captures in where they belong; end with the next small step when one is known.
Return {{}} when the card already covers these captures.""")
    r, made = _json_obj(reply), 0
    body = str(r.get("body", "")).strip()
    cur = p["body"].strip()
    if body and body != cur and not (len(cur) > 400 and len(body) < len(cur) // 2):   # a much shorter card means something was dropped - not proposed
        MN.card_propose(user, t["slug"], body, reason=str(r.get("reason", ""))[:200], sources=[e["id"] for e in new])
        made = 1
    _state_patch(user, "cards", lambda v: {**v, t["slug"]: t["last_capture"]})
    log(user, "card_review", thread=t["slug"], proposals=made, raw=reply[:2000] if not made and r else "")
    return made

# --- Notes that no longer matter: proposed for Reduce, with the reason ---

_MOOT_SYS = "You help a person let go of old notes. You point out notes that no longer matter, and say why in a few words. You answer with a JSON list only, no other text."

def _moot_candidates(user: str) -> list:
    """The person's own captures at least moot_days old, not in a card, with no open next step, not proposed before and not looked at before - oldest first, up to 25."""
    days, st = int(config()["moot_days"]), _state(user)
    if not days: return []
    asked, cut = {m["entry"] for m in MN.moots(user, "")} | set(st["mooted"]), (datetime.now() - timedelta(days=days)).isoformat()
    return [e for e in reversed(MN.entries(user)) if e["t"] < cut and e.get("author", "user") == "user" and not e.get("plain") and not e["absorbed"] and e["id"] not in asked and len(e["done"]) == len(e["next"])][:25]

def _kept_text(user: str) -> str:
    texts = {e["id"]: e["text"] for e in MN.entries(user)}
    return "\n".join(f"""- kept: "{texts.get(m["entry"], "")[:100]}"{" - " + m["reply"] if m.get("reply") else ""}""" for m in MN.moots(user, "kept")[-10:]) or "(none yet)"

async def propose_moot(user: str) -> int:
    cands = _moot_candidates(user)
    if not cands: return 0
    lines = "\n".join(f"""[{e["id"]}] ({e["t"][:10]}; threads: {", ".join(e["threads"]) or "none"}; {"next steps all done" if e["next"] else "no next steps"}) {e["text"][:300]}""" for e in cands)
    reply = await _llm(_MOOT_SYS, f"""{charter(user)}\n\nToday is {datetime.now().strftime("%Y-%m-%d (%A)")}.\n\nNotes the person kept when asked before (learn from them):\n{_kept_text(user)}\n\nOld notes - [id] (date; threads; next steps) text:\n{lines}\n
Return a JSON list with one object per note that clearly no longer matters: {{"entry": "<id in brackets>", "why": "<a few words: what made it no longer matter>"}}.
Only notes whose moment has passed or whose content was used up or replaced. Never notes that hold permanent information (accounts, setup, records, decisions still in force, story or research material). Return [] when unsure.""")
    by, made = {e["id"]: e for e in cands}, 0
    for r in _json_list(reply):
        if r.get("entry") in by and str(r.get("why", "")).strip(): MN.moot_propose(user, r["entry"], str(r["why"])); made += 1
    _state_patch(user, "mooted", lambda v: (v + sorted(by))[-3000:])   # looked at once
    log(user, "moot", notes=len(cands), proposed=made, raw=reply[:2000] if not made else "")
    return made

# --- Surveys: read-only agent runs for quiet threads ---

_SURVEY_SYS = "You pick which of a person's quiet threads (topics) would move forward if an assistant took a careful read-only look, and you write that task. You answer with a JSON list only, no other text."
_SURVEY_RULES = "This is a read-only survey for the person's notes: look, do not try to change anything. Finish with a short report: what you found (with file paths or sources), what is still unclear, and one concrete next step."

def agent() -> dict: return MN.ENV["services"].get("agent")   # the agent module's functions; None when it is not installed

def surveys(user: str, days: int = 3) -> list:
    """Surveys queued in the last days, newest first, each with its run's status (run is None once the run was deleted in Agent)."""
    since, svc = (datetime.now() - timedelta(days=days)).isoformat(), agent()
    return [{**s, "run": svc["run_status"](s["rid"]) if svc else None} for s in reversed(_state(user)["surveys"]) if s["t"] >= since]

def _last_active(user: str, t: dict) -> str: return max(t["last_capture"] or "", MN.page(user, t["slug"])["meta"].get("updated", ""))

def _stalled(user: str) -> list:
    """Threads with no capture or card change for stall_days and not surveyed within survey_gap_days, longest quiet first (up to 10)."""
    cfg, now = config(), datetime.now()
    quiet, recent = (now - timedelta(days=int(cfg["stall_days"]))).isoformat(), {s["slug"] for s in _state(user)["surveys"] if s["t"] >= (now - timedelta(days=int(cfg["survey_gap_days"]))).isoformat()}
    return sorted((t for t in MN.threads(user) if t["slug"] not in recent and "" < _last_active(user, t) < quiet), key=lambda t: _last_active(user, t))[:10]

def _brief(user: str, t: dict) -> str:
    nexts = "; ".join(n for e, i, n in MN.open_next(user, t["slug"])) or "none"
    return f"""#{t["slug"]} - {t["title"]} (last active {_last_active(user, t)[:10]})
card: {MN.page(user, t["slug"])["body"].strip()[:600] or "(empty)"}
open next steps: {nexts[:300]}"""

async def plan_surveys(user: str) -> int:
    """Queues read-only agent runs for quiet threads: at most survey_cap a day for this login, only when the login switched surveys on and an admin chose the agent settings.
    The model picks the threads and writes each task; read_only is always set, and the report comes back to the thread."""
    cfg, svc = config(), agent()
    if user not in cfg["survey_users"] or not cfg["survey_settings"] or not svc: return 0
    st, today = _state(user), datetime.now().date().isoformat()
    left = int(cfg["survey_cap"]) - sum(1 for s in st["surveys"] if s["t"][:10] == today)
    cands = _stalled(user) if left > 0 else []
    if not cands: return 0
    reply = await _llm(_SURVEY_SYS, f"""{charter(user)}

Today is {datetime.now().strftime("%Y-%m-%d (%A)")}.

What the assistant can reach, read-only: {svc["settings_summary"](cfg["survey_settings"])}

Quiet threads:
{chr(10).join(_brief(user, t) for t in cands)}

Return a JSON list of at most {left} objects, most useful first: {{"thread": "<slug>", "task": "<the task for the assistant: what to look at and what to report, a few sentences>", "why": "<few words>"}}.
Only threads where a read-only look with that access would clearly help the next step (finding where something is, summarising what exists, listing open questions, checking a fact). Return [] when none would.""")
    by, made = {t["slug"]: t for t in cands}, 0
    for r in _json_list(reply):
        t, task = by.pop(str(r.get("thread", "")), None), str(r.get("task", "")).strip()
        if not t or len(task) < 20 or made >= left: continue
        rec = svc["submit_run"](user, f"""{task}\n\n{_SURVEY_RULES}""", {**cfg["survey_settings"], "read_only": True, "muninn_thread": t["slug"]}, origin="huginn")
        st["surveys"].append({"t": MN._now(), "slug": t["slug"], "rid": rec["id"], "why": str(r.get("why", ""))[:200]})
        made += 1
    st["surveys"] = st["surveys"][-200:]
    _state_put(user, st)
    log(user, "surveys", candidates=len(cands), queued=made, raw=reply[:2000] if not made else "")
    return made

# --- Passes ---

async def run(user: str) -> dict:
    """One pass for one login: triage captures, judge dated and pinned next steps, review changed pads (open ones first, then the Pile), refresh changed thread cards, point out old notes that no longer matter,
    queue surveys for quiet threads. Errors are logged and end the pass."""
    if user in _RUNNING: return {"skipped": "already running"}
    _RUNNING.add(user)
    out = {"suggestions": 0, "signals": 0, "proposals": 0, "pads": 0, "cards": 0, "moot": 0, "surveys": 0}
    try:
        log(user, "pass_start", model=config()["model"] or "auto")
        out["suggestions"] = await triage(user)
        out["signals"] = await judge_signals(user)
        for m in _due_pads(user):
            out["proposals"] += await review_pad(user, m); out["pads"] += 1
            _state_patch(user, "pads", lambda v: {**v, m["id"]: MN.pad(user, m["id"])["version"]})
        for t in _due_cards(user): out["cards"] += await refresh_card(user, t)
        out["moot"] = await propose_moot(user)
        out["surveys"] = await plan_surveys(user)
        log(user, "pass_done", **out)
    except Exception as e:
        log(user, "pass_error", error=f"{type(e).__name__}: {e}"[:500]); print(f"[huginn] {user}: {e}", flush=True)
    finally: _RUNNING.discard(user)
    return out

async def _loop():
    while True:
        cfg = config()
        if cfg["enabled"]:
            for user in list(cfg["users"]): await run(user)
        await asyncio.sleep(max(5, int(cfg["interval_min"])) * 60)

def ensure_worker():
    """Starts the background loop once (called from requests, so it runs inside the server's event loop)."""
    global _TASK
    if _TASK is None or _TASK.done(): _TASK = asyncio.create_task(_loop())

def run_now(user: str):
    ensure_worker()
    t = asyncio.create_task(run(user))
    _RUNS.add(t); t.add_done_callback(_RUNS.discard)   # held: the event loop keeps only weak references to tasks
