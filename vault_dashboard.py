#!/usr/bin/env python3
"""
Vault Dashboard: your whole Obsidian vault on one screen.

Free, open source, no installs (Python standard library only).
Reads your vault folder and shows tasks, projects, recent notes, a graph,
full-text search and an "Ask your notes" box, styled like Obsidian.

Usage:
    python vault_dashboard.py                      # auto-finds your Obsidian vault
    python vault_dashboard.py "/path/to/My Vault"  # or point it at one
    python vault_dashboard.py --help

Optional free AI answers: install Ollama (https://ollama.com), run
`ollama pull llama3.2`, and keep Ollama running. Without it, "Ask your notes"
shows the most relevant passages instead.
"""

import argparse
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
UI_FILE = HERE / "dashboard.html"
IGNORED_DIRS = {".obsidian", ".trash", ".git", "node_modules", ".stfolder"}

TASK_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+\[(.)\]\s?(.*)$")
TAG_RE = re.compile(r"(?<![\w/&#])#([\w\-/]*[^\W\d][\w\-/]*)", re.UNICODE)
WIKILINK_RE = re.compile(r"(!?)\[\[([^\]\|#\^]*)(?:[#\^][^\]\|]*)?(?:\|[^\]]*)?\]\]")
MDLINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+?\.md)(?:#[^)]*)?\)")
DATE = r"(\d{4}-\d{2}-\d{2})"
DUE_RE = re.compile(r"(?:📅\ufe0f?|due::)\s*" + DATE)
SCHED_RE = re.compile(r"(?:⏳\ufe0f?|scheduled::)\s*" + DATE)
DONE_DATE_RE = re.compile(r"(?:✅|completion::)\s*" + DATE)
DAILY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
TASK_META_RE = re.compile(
    r"\s*(?:[📅⏳🛫✅➕❌]\ufe0f?\s*\d{4}-\d{2}-\d{2}|[⏫🔼🔽🔺⏬]\ufe0f?|🔁[^📅⏳🛫✅➕❌⏫🔼🔽🔺⏬]*"
    r"|\[(?:due|scheduled|start|completion|created|priority)::[^\]]*\])"
)
TASKS_PLUGIN_RE = re.compile(r"[📅⏳🛫🔁⏫🔼🔽🔺⏬➕]")
PRIORITY = {"🔺": 4, "⏫": 3, "🔼": 2, "🔽": -1, "⏬": -2}
STOPWORDS = set(
    "a an and are as at be but by can did do does for from had has have how i if in into is it its "
    "me my of on or our so that the their them then there these they this to was we were what when "
    "where which who why will with you your about any all also am been being just more most not "
    "should than too very would could tell show give list find".split()
)


# --------------------------------------------------------------------------- parsing
def _clean_scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def parse_frontmatter(text):
    """Return (properties, body, body_start_line). Handles the YAML Obsidian writes."""
    if not text.startswith("---"):
        return {}, text, 0
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not m:
        return {}, text, 0
    data, key = {}, None
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s*(.*)$", line) or (re.match(r"^-\s+(.*)$", line) if key else None)
        if item and key:
            if not isinstance(data.get(key), list):
                data[key] = [] if data.get(key) in (None, "") else [data[key]]
            data[key].append(_clean_scalar(item.group(1)))
            continue
        kv = re.match(r"^([^\s:][^:]*):\s*(.*)$", line)
        if kv:
            key, val = kv.group(1).strip(), kv.group(2).strip()
            if val == "":
                data[key] = []
            elif val.startswith("[") and val.endswith("]"):
                data[key] = [_clean_scalar(v) for v in val[1:-1].split(",") if v.strip()]
            else:
                data[key] = _clean_scalar(val)
    return data, text[m.end():], text[: m.end()].count("\n")


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r"[,\s]+", str(v)) if x.strip()]


def strip_markdown(s):
    s = re.sub(r"!\[\[[^\]]*\]\]", "", s)
    s = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"(\*\*|__|==|~~|`)", "", s)
    s = re.sub(r"^\s*(?:[-*+]|\d+[.)]|>+|#+)\s*(?:\[.\]\s*)?", "", s)
    return s.strip()


def parse_note(rel, text, st):
    fm, body, body_line = parse_frontmatter(text)
    lines = text.split("\n")
    tasks, kept, in_code = [], [], False
    for i in range(body_line, len(lines)):
        line = lines[i].rstrip("\r")
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = TASK_RE.match(line)
        if m:
            status, raw_text = m.group(3), m.group(4)
            due = DUE_RE.search(raw_text)
            sched = SCHED_RE.search(raw_text)
            done_on = DONE_DATE_RE.search(raw_text)
            prio = next((p for e, p in PRIORITY.items() if e in raw_text), 0)
            tasks.append({
                "line": i,
                "raw": line,
                "text": TASK_META_RE.sub("", raw_text).strip() or raw_text.strip(),
                "status": status,
                "done": status.lower() == "x" or status == "-",
                "cancelled": status == "-",
                "due": due.group(1) if due else None,
                "scheduled": sched.group(1) if sched else None,
                "done_on": done_on.group(1) if done_on else None,
                "priority": prio,
                "indent": len(m.group(1).replace("\t", "    ")),
            })
        kept.append(re.sub(r"`[^`]*`", "", line))
    clean = "\n".join(kept)

    tags = {t for t in TAG_RE.findall(clean)}
    for key in ("tags", "tag"):
        for t in _as_list(fm.get(key)):
            tags.add(t.lstrip("#"))
    links, embeds = [], []
    for bang, target in WIKILINK_RE.findall(clean):
        if target.strip():
            (embeds if bang else links).append(target.strip())
    links += [urllib.parse.unquote(t) for t in MDLINK_RE.findall(clean)]
    for v in fm.values():  # links inside properties count too, as in Obsidian
        for item in (v if isinstance(v, list) else [v]):
            links += [t.strip() for b, t in WIKILINK_RE.findall(str(item)) if t.strip()]

    excerpt = ""
    for para in re.split(r"\n\s*\n", clean):
        for ln in para.splitlines():
            if not ln.strip() or re.match(r"^\s*#{1,6}\s", ln) or TASK_RE.match(ln):
                continue
            s = strip_markdown(ln)
            if s and not s.startswith("---"):
                excerpt = s[:180]
                break
        if excerpt:
            break

    stem = Path(rel).stem
    parent = str(Path(rel).parent.as_posix())
    daily = DAILY_RE.match(stem)
    birth = getattr(st, "st_birthtime", None) or st.st_ctime
    return {
        "path": rel,
        "title": stem,
        "folder": "" if parent == "." else parent,
        "mtime": st.st_mtime,
        "ctime": min(birth, st.st_mtime),
        "words": len(re.findall(r"\S+", clean)),
        "tags": sorted(tags, key=str.lower),
        "excerpt": excerpt,
        "daily": daily.group(1) if daily else None,
        "fm": fm,
        "tasks": tasks,
        "_links": links,
        "_embeds": embeds,
        "_text": text,
        "_body": body,
        "_body_line": body_line,
        "_mtime_ns": st.st_mtime_ns,
        "_size": st.st_size,
    }


def is_project(n):
    tags = [t.lower() for t in n["tags"]]
    if any(t in ("project", "projects") or t.startswith("project/") for t in tags):
        return True
    for k in ("type", "category", "kind"):
        if str(n["fm"].get(k, "")).strip().lower() == "project":
            return True
    parts = [p.lower() for p in Path(n["path"]).parts[:-1]]
    if any("archive" in p for p in parts):
        return False
    is_pf = lambda p: re.fullmatch(r"[\d\W_]*projects?", p) is not None  # noqa: E731
    if parts and is_pf(parts[-1]):
        return True  # note sitting directly in a Projects folder
    if len(parts) >= 2 and is_pf(parts[-2]) and n["title"].lower() == parts[-1]:
        return True  # folder note: Projects/Alpha/Alpha.md
    return False


def tokenize(s):
    return [w for w in re.findall(r"\w+", s.lower()) if len(w) > 1]


# --------------------------------------------------------------------------- vault index
class Vault:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.name = self.root.name
        self.lock = threading.RLock()
        self.notes = {}
        self.attachments = {}
        self.version = 0
        self.last_scan = 0.0
        self._chunks = None

    # ---- scanning
    def scan(self, force=False):
        with self.lock:
            if not force and time.time() - self.last_scan < 1.5:
                return
            seen, changed, attachments = set(), False, {}
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in IGNORED_DIRS)
                for fn in filenames:
                    if fn.startswith("."):
                        continue
                    full = Path(dirpath) / fn
                    rel = full.relative_to(self.root).as_posix()
                    if not fn.lower().endswith(".md"):
                        attachments.setdefault(fn.lower(), rel)
                        continue
                    seen.add(rel)
                    try:
                        st = full.stat()
                    except OSError:
                        continue
                    old = self.notes.get(rel)
                    if old and old["_mtime_ns"] == st.st_mtime_ns and old["_size"] == st.st_size:
                        continue
                    try:
                        text = full.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    self.notes[rel] = parse_note(rel, text, st)
                    changed = True
            for rel in [r for r in self.notes if r not in seen]:
                del self.notes[rel]
                changed = True
            self.attachments = attachments
            if changed or self.version == 0:
                self._resolve_links()
                self.version += 1
                self._chunks = None
            self.last_scan = time.time()

    def _resolve_links(self):
        self.by_name, self.by_path = {}, {}
        for rel in sorted(self.notes, key=lambda r: (r.count("/"), r.lower())):
            self.by_name.setdefault(Path(rel).stem.lower(), rel)
            self.by_path[rel[:-3].lower()] = rel
        inbound = {rel: set() for rel in self.notes}
        for rel, n in self.notes.items():
            out, missing = [], []
            for target in n["_links"] + [e for e in n["_embeds"] if not Path(e).suffix or e.lower().endswith(".md")]:
                hit = self.resolve(target, rel)
                if hit:
                    if hit != rel and hit not in out:
                        out.append(hit)
                        inbound[hit].add(rel)
                elif target not in missing:
                    missing.append(target)
            n["links"], n["unresolved"] = out, missing
        for rel, n in self.notes.items():
            n["backlinks"] = sorted(inbound[rel])
            n["project"] = is_project(n)

    def resolve(self, target, from_rel=None):
        t = urllib.parse.unquote(target.strip()).replace("\\", "/")
        if t.lower().endswith(".md"):
            t = t[:-3]
        low = t.lower().lstrip("/")
        if not low:
            return None
        if low in self.by_path:
            return self.by_path[low]
        if from_rel and "/" in from_rel:
            rel_try = os.path.normpath(os.path.join(os.path.dirname(from_rel), t)).replace("\\", "/").lower()
            if rel_try in self.by_path:
                return self.by_path[rel_try]
        if "/" in low:
            for p, rel in self.by_path.items():
                if p.endswith("/" + low):
                    return rel
            low = low.rsplit("/", 1)[-1]
        return self.by_name.get(low)

    def safe_path(self, rel):
        full = (self.root / rel).resolve()
        if full != self.root and self.root not in full.parents:
            raise ValueError("Path is outside the vault.")
        return full

    # ---- API payloads
    def index_payload(self):
        self.scan()
        with self.lock:
            notes = [{k: v for k, v in n.items() if not k.startswith("_")} for n in self.notes.values()]
            tags = Counter(t for n in self.notes.values() for t in n["tags"])
            return {
                "vault": self.name,
                "root": str(self.root),
                "version": self.version,
                "today": date.today().isoformat(),
                "notes": notes,
                "tags": dict(tags.most_common()),
            }

    def note_payload(self, rel):
        self.scan()
        with self.lock:
            n = self.notes.get(rel)
            if not n:
                raise KeyError(rel)
            backlinks = []
            name = n["title"].lower()
            for src in n["backlinks"]:
                ctx = ""
                for ln in self.notes[src]["_body"].splitlines():
                    if name in ln.lower() and ("[[" in ln or "](" in ln):
                        ctx = strip_markdown(ln)[:220]
                        break
                backlinks.append({"path": src, "title": self.notes[src]["title"], "context": ctx})
            public = {k: v for k, v in n.items() if not k.startswith("_")}
            public.update(content=n["_text"], body_line=n["_body_line"], backlink_details=backlinks)
            return public

    def search(self, q, limit=40):
        self.scan()
        q = q.strip()
        with self.lock:
            notes = list(self.notes.values())
            if not q:
                notes.sort(key=lambda n: -n["mtime"])
                return [self._hit(n, "", "") for n in notes[:20]]
            if q.startswith("#") and len(q) > 1:
                tag = q[1:].lower()
                hits = [n for n in notes if any(t.lower() == tag or t.lower().startswith(tag + "/") or t.lower().startswith(tag) for t in n["tags"])]
                hits.sort(key=lambda n: -n["mtime"])
                return [self._hit(n, "#" + q[1:], "tag") for n in hits[:limit]]
            terms = [t for t in q.lower().split() if t]
            scored = []
            for n in notes:
                title, path, text = n["title"].lower(), n["path"].lower(), n["_body"].lower()
                score = 0
                for t in terms:
                    in_title, in_path, count = t in title, t in path, text.count(t)
                    if not (in_title or in_path or count):
                        break
                    score += (12 if in_title else 0) + (3 if in_path else 0) + min(count, 10)
                else:
                    if title == q.lower():
                        score += 60
                    elif title.startswith(q.lower()):
                        score += 25
                    scored.append((score, n))
            scored.sort(key=lambda x: (-x[0], -x[1]["mtime"]))
            return [self._hit(n, terms[0], "content") for _, n in scored[:limit]]

    def _hit(self, n, term, kind):
        snippet = n["excerpt"]
        if term:
            body = n["_body"]
            i = body.lower().find(term.lower())
            if i >= 0:
                a, b = max(0, i - 70), min(len(body), i + len(term) + 110)
                snippet = ("…" if a else "") + re.sub(r"\s+", " ", body[a:b]).strip() + ("…" if b < len(body) else "")
        return {"path": n["path"], "title": n["title"], "folder": n["folder"], "snippet": snippet, "kind": kind}

    # ---- task toggling (the only write the app ever makes)
    def toggle_task(self, rel, line_no, raw):
        full = self.safe_path(rel)
        if full.suffix.lower() != ".md" or not full.is_file():
            raise ValueError("Not a note.")
        with self.lock:
            text = full.read_bytes().decode("utf-8")
            lines = text.split("\n")
            want = raw.rstrip("\r")
            idx = None
            if 0 <= line_no < len(lines) and lines[line_no].rstrip("\r") == want:
                idx = line_no
            else:
                matches = [i for i, ln in enumerate(lines) if ln.rstrip("\r") == want]
                if len(matches) == 1:
                    idx = matches[0]
            if idx is None:
                raise ValueError("That note changed since it was loaded. Refresh and try again.")
            cr = "\r" if lines[idx].endswith("\r") else ""
            line = lines[idx].rstrip("\r")
            m = TASK_RE.match(line)
            if not m:
                raise ValueError("That line isn't a task.")
            checking = m.group(3).lower() != "x"
            pos = m.start(3)
            new = line[:pos] + ("x" if checking else " ") + line[pos + 1:]
            if checking and TASKS_PLUGIN_RE.search(new) and "✅" not in new:
                new += f" ✅ {date.today().isoformat()}"  # same convention as the Tasks plugin
            if not checking:
                new = re.sub(r"\s*✅\s*\d{4}-\d{2}-\d{2}", "", new)
            lines[idx] = new + cr
            tmp = full.with_name(f".{full.name}.vault-dashboard.tmp")
            tmp.write_bytes("\n".join(lines).encode("utf-8"))
            os.replace(tmp, full)
            self.scan(force=True)
            return {"line": idx, "raw": new, "done": checking}

    # ---- retrieval for "Ask your notes"
    def _build_chunks(self):
        chunks = []
        for rel, n in self.notes.items():
            buf, size = [], 0
            paras = [p.strip() for p in re.split(r"\n\s*\n", n["_body"]) if p.strip()]
            for p in paras:
                if buf and size + len(p) > 900:
                    chunks.append((rel, "\n\n".join(buf)))
                    buf, size = [], 0
                buf.append(p)
                size += len(p)
            if buf:
                chunks.append((rel, "\n\n".join(buf)))
            elif not paras:
                chunks.append((rel, ""))
        docs = []
        df = Counter()
        for rel, text in chunks:
            n = self.notes[rel]
            toks = tokenize(n["title"]) * 3 + tokenize(" ".join(n["tags"])) * 2 + tokenize(text)
            tf = Counter(toks)
            df.update(tf.keys())
            docs.append({"path": rel, "text": text, "tf": tf, "len": max(len(toks), 1)})
        avg = sum(d["len"] for d in docs) / max(len(docs), 1)
        self._chunks = (docs, df, avg)

    def retrieve(self, question, k=6):
        self.scan()
        with self.lock:
            if self._chunks is None:
                self._build_chunks()
            docs, df, avg = self._chunks
            q = [t for t in tokenize(question) if t not in STOPWORDS] or tokenize(question)
            N = len(docs) or 1
            scored = []
            for d in docs:
                s = 0.0
                for t in q:
                    f = d["tf"].get(t)
                    if not f:
                        continue
                    idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
                    s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * d["len"] / avg))
                if s > 0:
                    scored.append((s, d))
            scored.sort(key=lambda x: -x[0])
            out, per_note = [], Counter()
            for s, d in scored:
                if per_note[d["path"]] >= 2:
                    continue
                per_note[d["path"]] += 1
                n = self.notes[d["path"]]
                out.append({"path": d["path"], "title": n["title"], "text": d["text"][:1600], "score": round(s, 2)})
                if len(out) >= k:
                    break
            return out, q

    def task_digest(self, limit=40):
        today = date.today().isoformat()
        rows = []
        for n in self.notes.values():
            for t in n["tasks"]:
                if not t["done"]:
                    rows.append((t["due"] or "9999", n["title"], t))
        rows.sort(key=lambda r: r[0])
        out = []
        for due, title, t in rows[:limit]:
            when = "" if due == "9999" else (" (OVERDUE, due " + due + ")" if due < today else " (due " + due + ")")
            out.append(f"- {t['text']}{when} [[{title}]]")
        return "\n".join(out)

    def project_digest(self):
        out = []
        for n in self.notes.values():
            if n.get("project"):
                total = len(n["tasks"])
                done = sum(t["done"] for t in n["tasks"])
                status = n["fm"].get("status", "")
                out.append(f"- [[{n['title']}]] status: {status or 'n/a'}, tasks {done}/{total} done")
        return "\n".join(out)


# --------------------------------------------------------------------------- local AI (Ollama, free)
class LocalAI:
    def __init__(self, url, model):
        self.url = url.rstrip("/")
        self.wanted = model
        self._status = (0.0, None)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def status(self):
        ts, cached = self._status
        if time.time() - ts < 20:
            return cached
        info = {"available": False, "model": None, "engine": "Ollama"}
        try:
            with self.opener.open(self.url + "/api/tags", timeout=1.5) as r:
                models = [m.get("name", "") for m in json.load(r).get("models", [])]
            if models:
                pick = next((m for m in models if m == self.wanted or m.split(":")[0] == self.wanted.split(":")[0]), models[0])
                info.update(available=True, model=pick)
        except Exception:  # noqa: BLE001
            pass
        self._status = (time.time(), info)
        return info

    def chat(self, system, user):
        st = self.status()
        payload = json.dumps({
            "model": st["model"],
            "stream": False,
            "options": {"temperature": 0.2},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(self.url + "/api/chat", data=payload, headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=240) as r:
            return json.load(r)["message"]["content"].strip()


SYSTEM_PROMPT = (
    "You answer questions about the user's personal notes (an Obsidian vault). Use only the note excerpts "
    "and lists provided. When you use something from a note, cite it with its wiki link, like [[Note Title]]. "
    "If the notes don't contain the answer, say so in one sentence. Be concise and use short bullet lists "
    "for multiple items. Today's date is {today}."
)


# --------------------------------------------------------------------------- HTTP server
class App:
    vault: Vault = None
    ai: LocalAI = None
    port = 0
    hosts = set()
    origins = set()


class Handler(BaseHTTPRequestHandler):
    server_version = "VaultDashboard/1.0"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    def _host_ok(self):
        return self.headers.get("Host", "") in App.hosts

    def do_GET(self):
        if not self._host_ok():
            return self._err(403, "Forbidden host")
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        arg = lambda k, d="": qs.get(k, [d])[0]  # noqa: E731
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/vault":
                data = App.vault.index_payload()
                data["ai"] = App.ai.status()
                return self._send(200, data)
            if u.path == "/api/note":
                return self._send(200, App.vault.note_payload(arg("path")))
            if u.path == "/api/search":
                return self._send(200, App.vault.search(arg("q")))
            if u.path == "/api/file":
                return self._serve_file(arg("path"), arg("from"))
        except KeyError:
            return self._err(404, "Note not found")
        except ValueError as e:
            return self._err(400, str(e))
        except Exception as e:  # noqa: BLE001
            return self._err(500, f"{type(e).__name__}: {e}")
        self._err(404, "Not found")

    def _serve_file(self, rel, from_rel):
        v = App.vault
        rel = urllib.parse.unquote(rel).split("|")[0].strip()
        candidates = [rel]
        if from_rel:
            candidates.append(os.path.join(os.path.dirname(from_rel), rel))
        hit = None
        for c in candidates:
            try:
                p = v.safe_path(c)
            except ValueError:
                continue
            if p.is_file():
                hit = p
                break
        if hit is None and Path(rel).name.lower() in v.attachments:
            hit = v.safe_path(v.attachments[Path(rel).name.lower()])
        if hit is None:
            return self._err(404, "File not found")
        ext = hit.suffix.lower()
        types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                 ".webp": "image/webp", ".svg": "image/svg+xml", ".bmp": "image/bmp", ".avif": "image/avif",
                 ".pdf": "application/pdf", ".mp3": "audio/mpeg", ".mp4": "video/mp4", ".webm": "video/webm"}
        ctype = types.get(ext, "application/octet-stream")
        data = hit.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=60")
        self.send_header("X-Content-Type-Options", "nosniff")
        if ext == ".svg":
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self._host_ok():
            return self._err(403, "Forbidden host")
        origin = self.headers.get("Origin")
        if origin and origin not in App.origins:
            return self._err(403, "Forbidden origin")
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self._err(415, "JSON only")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                return self._err(413, "Too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            u = urllib.parse.urlparse(self.path)
            if u.path == "/api/toggle":
                return self._send(200, App.vault.toggle_task(body["path"], int(body["line"]), body["raw"]))
            if u.path == "/api/ask":
                return self._send(200, self._ask(str(body.get("question", "")).strip()))
        except (ValueError, KeyError, UnicodeDecodeError) as e:
            return self._err(400, str(e))
        except Exception as e:  # noqa: BLE001
            return self._err(500, f"{type(e).__name__}: {e}")
        self._err(404, "Not found")

    def _ask(self, question):
        if not question:
            raise ValueError("Ask a question first.")
        v, ai = App.vault, App.ai
        sources, terms = v.retrieve(question)
        st = ai.status()
        result = {"question": question, "sources": sources, "terms": terms, "mode": "search", "answer": None, "model": None}
        if not st["available"]:
            return result
        ql = question.lower()
        extra = ""
        if re.search(r"task|todo|to-do|to do|due|overdue|deadline|week|today|tomorrow", ql):
            extra += "\n\nOpen tasks across the vault:\n" + (v.task_digest() or "(none)")
        if "project" in ql:
            extra += "\n\nProjects:\n" + (v.project_digest() or "(none)")
        ctx = "\n\n".join(f"### [[{s['title']}]] ({s['path']})\n{s['text']}" for s in sources) or "(no matching notes)"
        prompt = f"Note excerpts:\n\n{ctx}{extra}\n\nQuestion: {question}"
        try:
            answer = ai.chat(SYSTEM_PROMPT.format(today=date.today().isoformat()), prompt)
            result.update(mode="ai", answer=answer, model=st["model"])
        except Exception as e:  # noqa: BLE001
            result["error"] = f"Ollama didn't answer ({type(e).__name__}). Showing matching passages instead."
        return result


# --------------------------------------------------------------------------- startup
def obsidian_config_paths():
    home = Path.home()
    paths = [
        home / "Library/Application Support/obsidian/obsidian.json",
        home / ".config/obsidian/obsidian.json",
        home / ".var/app/md.obsidian.Obsidian/config/obsidian/obsidian.json",
        home / "snap/obsidian/current/.config/obsidian/obsidian.json",
    ]
    if os.getenv("APPDATA"):
        paths.insert(0, Path(os.environ["APPDATA"]) / "obsidian/obsidian.json")
    return paths


def known_vaults():
    found = []
    for cfg in obsidian_config_paths():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for v in data.get("vaults", {}).values():
            p = Path(v.get("path", ""))
            if p.is_dir():
                found.append((bool(v.get("open")), v.get("ts", 0), p))
    found.sort(key=lambda x: (not x[0], -x[1]))
    seen, out = set(), []
    for _, _, p in found:
        if str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    return out


def choose_vault(arg):
    if arg:
        return Path(arg).expanduser()
    if os.getenv("VAULT_PATH"):
        return Path(os.environ["VAULT_PATH"]).expanduser()
    vaults = known_vaults()
    if len(vaults) == 1:
        return vaults[0]
    if vaults:
        print("\nObsidian vaults found on this computer:")
        for i, p in enumerate(vaults, 1):
            print(f"  {i}. {p}")
        pick = input(f"Which one? [1-{len(vaults)}, Enter = 1]: ").strip() or "1"
        if pick.isdigit() and 1 <= int(pick) <= len(vaults):
            return vaults[int(pick) - 1]
        return Path(pick).expanduser()
    return Path(input("Path to your Obsidian vault folder: ").strip().strip('"').strip("'")).expanduser()


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="Your Obsidian vault on one screen.")
    ap.add_argument("vault", nargs="?", help="Path to the vault folder (auto-detected if left out)")
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")))
    ap.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "llama3.2"), help="Ollama model for answers")
    ap.add_argument("--ollama-url", default=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"))
    ap.add_argument("--no-browser", action="store_true", help="Don't open the browser automatically")
    args = ap.parse_args()

    if not UI_FILE.exists():
        sys.exit(f"Missing {UI_FILE.name}. Keep it in the same folder as this script.")
    root = choose_vault(args.vault)
    if not root.is_dir():
        sys.exit(f"Folder not found: {root}")

    App.vault = Vault(root)
    t0 = time.time()
    App.vault.scan(force=True)
    App.ai = LocalAI(args.ollama_url, args.model)

    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit("No free port found. Try --port 9000")
    server.daemon_threads = True
    App.port = server.server_address[1]
    App.hosts = {f"127.0.0.1:{App.port}", f"localhost:{App.port}"}
    App.origins = {f"http://{h}" for h in App.hosts}

    url = f"http://127.0.0.1:{App.port}"
    ai = App.ai.status()
    print(f"\n  Vault Dashboard")
    print(f"  Vault:   {App.vault.root}  ({len(App.vault.notes)} notes, indexed in {time.time() - t0:.1f}s)")
    print(f"  AI:      {'Ollama / ' + ai['model'] if ai['available'] else 'off (install Ollama for AI answers)'}")
    print(f"  Open:    {url}")
    print("  Stop:    Ctrl+C\n")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
        server.server_close()


if __name__ == "__main__":
    main()
