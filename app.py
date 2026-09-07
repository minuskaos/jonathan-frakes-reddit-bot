import hmac
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from collections import deque
from copy import deepcopy
from pathlib import Path
import praw
from flask import Flask, Response, jsonify, render_template_string, request
from prawcore.exceptions import PrawcoreException
from waitress import serve

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
DEFAULT_CONFIG_PATH = APP_DIR / "default-config.json"
DB_PATH = DATA_DIR / "state.db"
LOG_PATH = DATA_DIR / "bot.log"
WEB_PORT = int(os.getenv("WEB_PORT", "8787"))

DEFAULT_CONFIG = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


class MemoryLogHandler(logging.Handler):
    def __init__(self, maxlen=300):
        super().__init__()
        self.lines = deque(maxlen=maxlen)
        self.lock2 = threading.Lock()

    def emit(self, record):
        try:
            line = self.format(record)
            with self.lock2:
                self.lines.append(line)
        except Exception:
            pass

    def snapshot(self, limit=150):
        with self.lock2:
            return list(self.lines)[-limit:]


log = logging.getLogger("frakesbot")
log.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
log.propagate = False
formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
if not log.handlers:
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    log.addHandler(sh)
    try:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(formatter)
        log.addHandler(fh)
    except Exception:
        pass
    memory_handler = MemoryLogHandler()
    memory_handler.setFormatter(formatter)
    log.addHandler(memory_handler)
else:
    memory_handler = next((h for h in log.handlers if isinstance(h, MemoryLogHandler)), MemoryLogHandler())


class ConfigManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.config = self._load()

    def _load(self):
        source = CONFIG_PATH if CONFIG_PATH.exists() else DEFAULT_CONFIG_PATH
        data = deepcopy(DEFAULT_CONFIG)
        if source.exists():
            try:
                with source.open("r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    # Migrate v2 single-keyword configs automatically.
                    if "keywords" not in loaded and loaded.get("keyword"):
                        loaded["keywords"] = [loaded["keyword"]]
                    data.update(loaded)
            except Exception as exc:
                log.error("Could not load config: %s", exc)
        self._validate(data)
        if not CONFIG_PATH.exists():
            self._atomic_write(data)
        return data

    def _validate(self, c):
        keywords = c.get("keywords", ["Jonathan Frakes"])
        if isinstance(keywords, str):
            keywords = keywords.splitlines()
        if not isinstance(keywords, list):
            keywords = []
        # De-duplicate while preserving display order.
        seen = set()
        clean_keywords = []
        for value in keywords:
            value = str(value).strip()
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                clean_keywords.append(value)
        if not clean_keywords:
            raise ValueError("At least one keyword is required")
        c["keywords"] = clean_keywords
        c.pop("keyword", None)
        c["match_mode"] = c.get("match_mode", "contains")
        if c["match_mode"] not in {"contains", "whole_word", "exact"}:
            c["match_mode"] = "contains"
        c["subreddits"] = str(c.get("subreddits", "all")).strip() or "all"
        c["context_url"] = str(c.get("context_url", "")).strip()
        c["context_label"] = str(c.get("context_label", "Context")).strip() or "Context"
        c["reply_prefix"] = str(c.get("reply_prefix", ""))
        c["reply_suffix"] = str(c.get("reply_suffix", ""))
        c["reply_probability_percent"] = max(0, min(100, int(c.get("reply_probability_percent", 100))))
        c["avoid_recent_questions"] = max(0, min(1000, int(c.get("avoid_recent_questions", 5))))
        c["max_replies_per_hour"] = max(0, int(c.get("max_replies_per_hour", 20)))
        c["max_replies_per_day"] = max(0, int(c.get("max_replies_per_day", 100)))
        c["min_seconds_between_replies"] = max(0, int(c.get("min_seconds_between_replies", 30)))
        for key in ["blacklist_subreddits", "blacklist_users", "questions"]:
            value = c.get(key, [])
            if not isinstance(value, list):
                value = []
            c[key] = [str(x).strip() for x in value if str(x).strip()]
        if not c["questions"]:
            raise ValueError("At least one question is required")
        for key in [
            "bot_enabled", "dry_run", "case_sensitive", "monitor_comments",
            "monitor_submissions", "context_enabled", "one_reply_per_thread"
        ]:
            c[key] = bool(c.get(key, DEFAULT_CONFIG[key]))
        return c

    def _atomic_write(self, data):
        temp = CONFIG_PATH.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        temp.replace(CONFIG_PATH)

    def get(self):
        with self.lock:
            return deepcopy(self.config)

    def save(self, data):
        with self.lock:
            merged = deepcopy(DEFAULT_CONFIG)
            merged.update(data)
            self._validate(merged)
            self._atomic_write(merged)
            self.config = merged
            return deepcopy(merged)


class DataStore:
    STAT_KEYS = [
        "items_seen", "comments_seen", "submissions_seen", "matches_found",
        "replies_sent", "dry_run_matches", "skipped_duplicate", "skipped_blacklist",
        "skipped_self", "skipped_thread", "skipped_probability", "skipped_rate_limit", "errors"
    ]

    def __init__(self):
        self.lock = threading.RLock()
        self.stats = {k: 0 for k in self.STAT_KEYS}
        self._init_db()
        self._load_stats()
        threading.Thread(target=self._flush_loop, daemon=True, name="stats-flush").start()

    def connect(self):
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS processed (
                    fullname TEXT PRIMARY KEY,
                    thread_id TEXT,
                    outcome TEXT NOT NULL,
                    processed_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_processed_thread ON processed(thread_id, outcome);
                CREATE INDEX IF NOT EXISTS idx_processed_time ON processed(processed_at, outcome);
                CREATE TABLE IF NOT EXISTS stats (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS keyword_stats (
                    keyword TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    subreddit TEXT,
                    author TEXT,
                    permalink TEXT,
                    detail TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(ts DESC);
            """)

    def _load_stats(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM stats").fetchall()
        with self.lock:
            for row in rows:
                if row["key"] in self.stats:
                    self.stats[row["key"]] = int(row["value"])

    def inc(self, key, amount=1):
        with self.lock:
            self.stats[key] = self.stats.get(key, 0) + amount

    def inc_keyword(self, keyword, amount=1):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO keyword_stats(keyword,value) VALUES(?,?) "
                "ON CONFLICT(keyword) DO UPDATE SET value=value+excluded.value",
                (keyword, amount),
            )

    def keyword_stats_snapshot(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT keyword,value FROM keyword_stats ORDER BY value DESC, keyword COLLATE NOCASE").fetchall()
        return {row["keyword"]: int(row["value"]) for row in rows}

    def stats_snapshot(self):
        with self.lock:
            return dict(self.stats)

    def flush_stats(self):
        snap = self.stats_snapshot()
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO stats(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                list(snap.items()),
            )

    def _flush_loop(self):
        while True:
            time.sleep(10)
            try:
                self.flush_stats()
            except Exception as exc:
                log.warning("Stats flush failed: %s", exc)

    def reset_stats(self):
        with self.lock:
            self.stats = {k: 0 for k in self.STAT_KEYS}
        self.flush_stats()
        with self.connect() as conn:
            conn.execute("DELETE FROM keyword_stats")

    def is_processed(self, fullname):
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM processed WHERE fullname=?", (fullname,)).fetchone() is not None

    def mark_processed(self, fullname, thread_id, outcome):
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed(fullname,thread_id,outcome,processed_at) VALUES(?,?,?,?)",
                (fullname, thread_id, outcome, int(time.time())),
            )

    def thread_has_reply(self, thread_id):
        if not thread_id:
            return False
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM processed WHERE thread_id=? AND outcome IN ('sent','dry_run') LIMIT 1",
                (thread_id,),
            ).fetchone() is not None

    def sent_count_since(self, seconds):
        cutoff = int(time.time() - seconds)
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM processed WHERE outcome='sent' AND processed_at>=?", (cutoff,)
            ).fetchone()[0])

    def seconds_since_last_sent(self):
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(processed_at) FROM processed WHERE outcome='sent'").fetchone()
        if not row or row[0] is None:
            return None
        return max(0, int(time.time() - int(row[0])))

    def add_activity(self, kind, subreddit="", author="", permalink="", detail=""):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO activity(ts,kind,subreddit,author,permalink,detail) VALUES(?,?,?,?,?,?)",
                (int(time.time()), kind, subreddit, author, permalink, detail[:2000]),
            )
            conn.execute("DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity ORDER BY id DESC LIMIT 1000)")

    def recent_activity(self, limit=100):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id,ts,kind,subreddit,author,permalink,detail FROM activity ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


class BotManager:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.lock = threading.RLock()
        self.generation = 0
        self.status = "stopped"
        self.connected_as = ""
        self.last_error = ""
        self.started_at = None
        self.last_match_at = None
        self.last_reply_at = None
        self.recent_questions = deque(maxlen=1000)

    def credentials_present(self):
        names = ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD"]
        return all(os.getenv(n) for n in names)

    def _reddit(self):
        username = os.environ["REDDIT_USERNAME"]
        return praw.Reddit(
            client_id=os.environ["REDDIT_CLIENT_ID"],
            client_secret=os.environ["REDDIT_CLIENT_SECRET"],
            username=username,
            password=os.environ["REDDIT_PASSWORD"],
            user_agent=os.getenv("REDDIT_USER_AGENT", f"linux:frakes-reddit-bot:v2.0 (by /u/{username})"),
        )

    def restart(self):
        with self.lock:
            self.generation += 1
            gen = self.generation
            config = self.cfg.get()
            self.status = "stopped"
            self.connected_as = ""
            self.last_error = ""
            self.started_at = None
        if not config["bot_enabled"]:
            log.info("Bot disabled from dashboard")
            return
        if not self.credentials_present():
            with self.lock:
                self.status = "credentials missing"
                self.last_error = "Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME and REDDIT_PASSWORD."
            log.warning("Reddit credentials are missing; dashboard will remain available")
            return
        threading.Thread(target=self._supervise, args=(gen,), daemon=True, name=f"bot-{gen}").start()

    def stop(self):
        with self.lock:
            self.generation += 1
            self.status = "stopped"
            self.started_at = None
        log.info("Bot stopped")

    def _active(self, gen):
        with self.lock:
            return gen == self.generation and self.cfg.get()["bot_enabled"]

    def _supervise(self, gen):
        try:
            with self.lock:
                if gen != self.generation:
                    return
                self.status = "connecting"
            reddit = self._reddit()
            me = str(reddit.user.me())
            with self.lock:
                if gen != self.generation:
                    return
                self.status = "running"
                self.connected_as = me
                self.started_at = int(time.time())
            log.info("Connected as u/%s", me)
            config = self.cfg.get()
            workers = []
            if config["monitor_comments"]:
                workers.append(threading.Thread(target=self._stream_worker, args=(gen, "comments"), daemon=True, name=f"comments-{gen}"))
            if config["monitor_submissions"]:
                workers.append(threading.Thread(target=self._stream_worker, args=(gen, "submissions"), daemon=True, name=f"submissions-{gen}"))
            for t in workers:
                t.start()
            if not workers:
                with self.lock:
                    self.status = "idle"
                log.warning("Both comment and submission monitoring are disabled")
            while self._active(gen):
                time.sleep(1)
        except Exception as exc:
            self.store.inc("errors")
            self.store.add_activity("error", detail=str(exc))
            with self.lock:
                if gen == self.generation:
                    self.status = "error"
                    self.last_error = str(exc)
            log.exception("Could not connect to Reddit")

    def _stream_worker(self, gen, kind):
        while self._active(gen):
            try:
                reddit = self._reddit()
                config = self.cfg.get()
                subreddit = reddit.subreddit(config["subreddits"])
                stream = subreddit.stream.comments if kind == "comments" else subreddit.stream.submissions
                log.info("Watching %s in r/%s", kind, config["subreddits"])
                for item in stream(skip_existing=True, pause_after=-1):
                    if not self._active(gen):
                        return
                    if item is None:
                        time.sleep(0.5)
                        continue
                    self._process(item, kind)
            except PrawcoreException as exc:
                self.store.inc("errors")
                self._set_error(str(exc))
                log.warning("Reddit API error in %s stream: %s", kind, exc)
                time.sleep(15)
            except Exception as exc:
                self.store.inc("errors")
                self._set_error(str(exc))
                log.exception("Unexpected %s stream error", kind)
                time.sleep(15)

    def _set_error(self, text):
        with self.lock:
            self.last_error = text

    @staticmethod
    def _item_text(item, kind):
        if kind == "submissions":
            return f"{getattr(item, 'title', '')}\n{getattr(item, 'selftext', '')}"
        return getattr(item, "body", "")

    @staticmethod
    def _matching_keywords(text, config):
        matched = []
        mode = config["match_mode"]
        case_sensitive = config["case_sensitive"]
        text_cmp = text if case_sensitive else text.casefold()
        flags = 0 if case_sensitive else re.IGNORECASE
        for keyword in config["keywords"]:
            key_cmp = keyword if case_sensitive else keyword.casefold()
            if mode == "exact":
                ok = text_cmp.strip() == key_cmp
            elif mode == "whole_word":
                ok = re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", text, flags) is not None
            else:
                ok = key_cmp in text_cmp
            if ok:
                matched.append(keyword)
        return matched

    def _thread_id(self, item, kind):
        if kind == "submissions":
            return getattr(item, "id", "")
        try:
            return getattr(item, "link_id", "").replace("t3_", "")
        except Exception:
            return ""

    def _choose_question(self, config):
        questions = config["questions"]
        avoid_n = min(config["avoid_recent_questions"], max(0, len(questions) - 1))
        blocked = set(list(self.recent_questions)[-avoid_n:]) if avoid_n else set()
        choices = [q for q in questions if q not in blocked] or questions
        q = random.choice(choices)
        self.recent_questions.append(q)
        return q

    def make_reply(self, config=None):
        c = config or self.cfg.get()
        question = self._choose_question(c)
        pieces = []
        if c["reply_prefix"].strip():
            pieces.append(c["reply_prefix"].strip())
        pieces.append(question)
        if c["context_enabled"] and c["context_url"]:
            pieces.append(f"[{c['context_label']}]({c['context_url']})")
        if c["reply_suffix"].strip():
            pieces.append(c["reply_suffix"].strip())
        return "\n\n".join(pieces), question

    def _process(self, item, kind):
        c = self.cfg.get()
        self.store.inc("items_seen")
        self.store.inc("comments_seen" if kind == "comments" else "submissions_seen")
        text = self._item_text(item, kind)
        matched_keywords = self._matching_keywords(text, c)
        if not matched_keywords:
            return

        self.store.inc("matches_found")
        for keyword in matched_keywords:
            self.store.inc_keyword(keyword)
        self.last_match_at = int(time.time())
        fullname = getattr(item, "fullname", "")
        subreddit = str(getattr(item, "subreddit", ""))
        author = str(getattr(item, "author", "") or "")
        permalink = f"https://reddit.com{getattr(item, 'permalink', '')}"
        thread_id = self._thread_id(item, kind)

        if fullname and self.store.is_processed(fullname):
            self.store.inc("skipped_duplicate")
            return

        black_subs = {x.lower() for x in c["blacklist_subreddits"]}
        black_users = {x.lower().lstrip("u/") for x in c["blacklist_users"]}
        if subreddit.lower() in black_subs or author.lower().lstrip("u/") in black_users:
            self.store.inc("skipped_blacklist")
            self.store.mark_processed(fullname, thread_id, "blacklist")
            self.store.add_activity("blacklist", subreddit, author, permalink, "Matched but was blacklisted")
            return

        own_user = os.getenv("REDDIT_USERNAME", "").lower()
        if author.lower() == own_user:
            self.store.inc("skipped_self")
            self.store.mark_processed(fullname, thread_id, "self")
            return

        if c["one_reply_per_thread"] and self.store.thread_has_reply(thread_id):
            self.store.inc("skipped_thread")
            self.store.mark_processed(fullname, thread_id, "thread")
            self.store.add_activity("thread-skip", subreddit, author, permalink, "Already replied in this thread")
            return

        if random.uniform(0, 100) > c["reply_probability_percent"]:
            self.store.inc("skipped_probability")
            self.store.mark_processed(fullname, thread_id, "probability")
            self.store.add_activity("probability-skip", subreddit, author, permalink, "Skipped by reply probability")
            return

        reply, question = self.make_reply(c)
        if c["dry_run"]:
            self.store.inc("dry_run_matches")
            self.store.mark_processed(fullname, thread_id, "dry_run")
            self.store.add_activity("dry-run", subreddit, author, permalink, question)
            log.info("DRY RUN match in r/%s by u/%s: %s | %s", subreddit, author, permalink, question)
            return

        hour_limit = c["max_replies_per_hour"]
        day_limit = c["max_replies_per_day"]
        min_gap = c["min_seconds_between_replies"]
        limited = False
        reason = ""
        if hour_limit and self.store.sent_count_since(3600) >= hour_limit:
            limited, reason = True, "Hourly reply cap reached"
        elif day_limit and self.store.sent_count_since(86400) >= day_limit:
            limited, reason = True, "Daily reply cap reached"
        else:
            since = self.store.seconds_since_last_sent()
            if min_gap and since is not None and since < min_gap:
                limited, reason = True, f"Minimum reply gap not met ({since}s/{min_gap}s)"
        if limited:
            self.store.inc("skipped_rate_limit")
            self.store.mark_processed(fullname, thread_id, "rate_limit")
            self.store.add_activity("rate-limit", subreddit, author, permalink, reason)
            log.info("%s: %s", reason, permalink)
            return

        try:
            item.reply(reply)
            self.store.inc("replies_sent")
            self.store.mark_processed(fullname, thread_id, "sent")
            self.store.add_activity("reply", subreddit, author, permalink, question)
            self.last_reply_at = int(time.time())
            log.info("Replied in r/%s to u/%s: %s | %s", subreddit, author, permalink, question)
        except Exception as exc:
            self.store.inc("errors")
            self.store.add_activity("error", subreddit, author, permalink, str(exc))
            self._set_error(str(exc))
            log.exception("Reply failed: %s", permalink)

    def snapshot(self):
        with self.lock:
            uptime = int(time.time() - self.started_at) if self.started_at else 0
            return {
                "status": self.status,
                "connected_as": self.connected_as,
                "last_error": self.last_error,
                "started_at": self.started_at,
                "uptime_seconds": uptime,
                "last_match_at": self.last_match_at,
                "last_reply_at": self.last_reply_at,
                "credentials_present": self.credentials_present(),
            }


cfg = ConfigManager()
store = DataStore()
bot = BotManager(cfg, store)
app = Flask(__name__)

@app.before_request
def optional_basic_auth():
    password = os.getenv("UI_PASSWORD", "")
    if not password or request.path == "/health":
        return None
    username = os.getenv("UI_USERNAME", "frakes")
    auth = request.authorization
    if auth and hmac.compare_digest(auth.username or "", username) and hmac.compare_digest(auth.password or "", password):
        return None
    return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Frakes Bot"'})

PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jonathan Frakes Bot</title>
<style>
:root{color-scheme:dark;--bg:#0b0d10;--panel:#14181d;--panel2:#1b2027;--border:#2a313a;--text:#edf1f5;--muted:#96a0ac;--accent:#ff4500;--good:#35c96f;--warn:#f4bd4f;--bad:#ff5c5c}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}header{position:sticky;top:0;z-index:5;background:#0b0d10eF;border-bottom:1px solid var(--border);backdrop-filter:blur(8px)}.wrap{max-width:1400px;margin:auto;padding:18px 22px}.head{display:flex;gap:16px;align-items:center;justify-content:space-between}.title{font-size:23px;font-weight:750}.subtitle{color:var(--muted);font-size:12px}.status{display:flex;align-items:center;gap:8px;font-weight:650}.dot{width:10px;height:10px;border-radius:50%;background:var(--muted)}.dot.running{background:var(--good);box-shadow:0 0 10px #35c96f66}.dot.connecting{background:var(--warn)}.dot.error,.dot.credentials-missing{background:var(--bad)}.grid{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:18px 0}.card,.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px}.card{padding:14px}.metric{font-size:26px;font-weight:750;margin-top:4px}.label{color:var(--muted);font-size:12px}.layout{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(420px,.95fr);gap:16px}.panel{padding:18px;margin-bottom:16px}.panel h2{font-size:16px;margin:0 0 14px}.formgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.full{grid-column:1/-1}label{display:block;color:var(--muted);font-size:12px;margin-bottom:5px}input,select,textarea{width:100%;background:#0f1216;color:var(--text);border:1px solid var(--border);border-radius:8px;padding:9px 10px;font:inherit}textarea{resize:vertical;min-height:92px}.questions{min-height:300px}.row{display:flex;gap:10px;align-items:center}.switchrow{display:flex;gap:18px;flex-wrap:wrap;margin:4px 0}.check{display:flex;align-items:center;gap:7px;color:var(--text)}.check input{width:auto}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px}button{border:1px solid var(--border);background:var(--panel2);color:var(--text);border-radius:8px;padding:9px 12px;font-weight:650;cursor:pointer}button.primary{background:var(--accent);border-color:var(--accent)}button.danger{color:#ff9b9b}.hint{color:var(--muted);font-size:11px;margin-top:4px}.notice{padding:10px 12px;border-radius:8px;background:#18130a;border:1px solid #5e4512;color:#ffd77a;margin-bottom:14px;display:none}.preview{white-space:pre-wrap;background:#0f1216;border:1px solid var(--border);border-radius:8px;padding:12px;min-height:70px}.activity{max-height:420px;overflow:auto}.event{padding:10px 0;border-bottom:1px solid var(--border)}.event:last-child{border:0}.eventtop{display:flex;justify-content:space-between;gap:10px}.kind{font-weight:700}.small{font-size:12px;color:var(--muted)}a{color:#8bbcff;text-decoration:none}.logs{height:250px;overflow:auto;background:#090b0d;border:1px solid var(--border);border-radius:8px;padding:10px;white-space:pre-wrap;font:12px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace}.toast{position:fixed;right:20px;bottom:20px;background:#222a32;border:1px solid var(--border);padding:11px 14px;border-radius:9px;display:none;z-index:10}@media(max-width:1000px){.grid{grid-template-columns:repeat(3,1fr)}.layout{grid-template-columns:1fr}}@media(max-width:600px){.wrap{padding:14px}.grid{grid-template-columns:repeat(2,1fr)}.formgrid{grid-template-columns:1fr}.full{grid-column:auto}}
</style>
</head>
<body>
<header><div class="wrap head"><div><div class="title">Jonathan Frakes Bot</div><div class="subtitle">Reddit watcher dashboard</div></div><div class="status"><span id="dot" class="dot"></span><span id="status">Loading…</span></div></div></header>
<main class="wrap">
<div id="notice" class="notice"></div>
<div id="runtime" class="small" style="margin:4px 0 12px"></div>
<div class="grid">
  <div class="card"><div class="label">Items seen</div><div id="items_seen" class="metric">0</div></div>
  <div class="card"><div class="label">Comments</div><div id="comments_seen" class="metric">0</div></div>
  <div class="card"><div class="label">Posts</div><div id="submissions_seen" class="metric">0</div></div>
  <div class="card"><div class="label">Matches</div><div id="matches_found" class="metric">0</div></div>
  <div class="card"><div class="label">Replies sent</div><div id="replies_sent" class="metric">0</div></div>
  <div class="card"><div class="label">Dry-run matches</div><div id="dry_run_matches" class="metric">0</div></div>
  <div class="card"><div class="label">Thread skips</div><div id="skipped_thread" class="metric">0</div></div>
  <div class="card"><div class="label">Blacklist skips</div><div id="skipped_blacklist" class="metric">0</div></div>
  <div class="card"><div class="label">Rate-limited</div><div id="skipped_rate_limit" class="metric">0</div></div>
  <div class="card"><div class="label">Probability skips</div><div id="skipped_probability" class="metric">0</div></div>
  <div class="card"><div class="label">Duplicates</div><div id="skipped_duplicate" class="metric">0</div></div>
  <div class="card"><div class="label">Errors</div><div id="errors" class="metric">0</div></div>
</div>
<div class="layout">
<section>
  <div class="panel"><h2>Bot settings</h2>
    <div class="switchrow">
      <label class="check"><input id="bot_enabled" type="checkbox"> Bot enabled</label>
      <label class="check"><input id="dry_run" type="checkbox"> Dry run</label>
      <label class="check"><input id="monitor_comments" type="checkbox"> Comments</label>
      <label class="check"><input id="monitor_submissions" type="checkbox"> Posts</label>
    </div>
    <div class="formgrid">
      <div class="full"><label>Keywords, one per line</label><textarea id="keywords" placeholder="Jonathan Frakes&#10;Beyond Belief&#10;Fact or Fiction"></textarea><div class="hint">A post or comment matches when any configured keyword matches.</div></div>
      <div><label>Match mode</label><select id="match_mode"><option value="contains">Contains</option><option value="whole_word">Whole phrase boundary</option><option value="exact">Exact entire text</option></select></div>
      <div class="full"><label>Subreddits</label><input id="subreddits" placeholder="all or AskReddit+television"><div class="hint">Use all, one subreddit, or PRAW multi syntax such as television+AskReddit.</div></div>
      <div><label>Reply probability (%)</label><input id="reply_probability_percent" type="number" min="0" max="100"></div>
      <div><label>Avoid last N questions</label><input id="avoid_recent_questions" type="number" min="0"></div>
      <div><label>Max replies / hour</label><input id="max_replies_per_hour" type="number" min="0"></div>
      <div><label>Max replies / day</label><input id="max_replies_per_day" type="number" min="0"></div>
      <div><label>Minimum seconds between replies</label><input id="min_seconds_between_replies" type="number" min="0"></div>
      <div class="switchrow" style="align-self:end"><label class="check"><input id="case_sensitive" type="checkbox"> Case sensitive</label><label class="check"><input id="one_reply_per_thread" type="checkbox"> One reply per thread</label></div>
      <div><label>Blacklisted subreddits, one per line</label><textarea id="blacklist_subreddits"></textarea></div>
      <div><label>Blacklisted users, one per line</label><textarea id="blacklist_users"></textarea></div>
    </div>
  </div>

  <div class="panel"><h2>Reply format</h2>
    <div class="formgrid">
      <div class="full"><label>Context URL</label><input id="context_url"><div class="hint">One global URL. Change it here once and every question uses the new URL.</div></div>
      <div><label>Context link text</label><input id="context_label"></div>
      <div><label>&nbsp;</label><label class="check"><input id="context_enabled" type="checkbox"> Include context link</label></div>
      <div><label>Reply prefix</label><textarea id="reply_prefix"></textarea></div>
      <div><label>Reply suffix</label><textarea id="reply_suffix"></textarea></div>
    </div>
  </div>

  <div class="panel"><h2>Questions <span id="qcount" class="small"></span></h2><label>One question per line</label><textarea id="questions" class="questions"></textarea>
    <div class="actions"><button id="save" class="primary">Save settings</button><button id="preview">Preview random reply</button><button id="reset" class="danger">Reset counters</button></div>
  </div>
</section>
<section>
  <div class="panel"><h2>Keyword matches</h2><div id="keywordstats" class="activity"><div class="small">No matches yet.</div></div></div>
  <div class="panel"><h2>Random reply preview</h2><div id="previewbox" class="preview">Click “Preview random reply”.</div></div>
  <div class="panel"><h2>Recent activity</h2><div id="activity" class="activity"><div class="small">No activity yet.</div></div></div>
  <div class="panel"><h2>Live log</h2><div id="logs" class="logs"></div></div>
</section>
</div>
</main><div id="toast" class="toast"></div>
<script>
const ids=['bot_enabled','dry_run','keywords','match_mode','case_sensitive','subreddits','monitor_comments','monitor_submissions','context_enabled','context_url','context_label','reply_prefix','reply_suffix','reply_probability_percent','avoid_recent_questions','one_reply_per_thread','max_replies_per_hour','max_replies_per_day','min_seconds_between_replies','blacklist_subreddits','blacklist_users','questions'];
let loaded=false;
function toast(t){const e=document.getElementById('toast');e.textContent=t;e.style.display='block';setTimeout(()=>e.style.display='none',2200)}
function lines(v){return v.split('\n').map(x=>x.trim()).filter(Boolean)}
function formConfig(){const o={}; for(const id of ids){const e=document.getElementById(id); if(e.type==='checkbox')o[id]=e.checked; else if(e.type==='number')o[id]=Number(e.value||0); else if(['keywords','blacklist_subreddits','blacklist_users','questions'].includes(id))o[id]=lines(e.value); else o[id]=e.value;} return o}
function fill(c){for(const id of ids){const e=document.getElementById(id);if(!e||!(id in c))continue;if(e.type==='checkbox')e.checked=!!c[id];else if(Array.isArray(c[id]))e.value=c[id].join('\n');else e.value=c[id]??''}loaded=true;updateQCount()}
function updateQCount(){document.getElementById('qcount').textContent=`(${lines(document.getElementById('questions').value).length})`}
document.getElementById('questions').addEventListener('input',updateQCount);
async function loadConfig(){const r=await fetch('/api/config');fill(await r.json())}
function fmtDur(s){s=Number(s||0);if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m';if(s<86400)return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';return Math.floor(s/86400)+'d '+Math.floor((s%86400)/3600)+'h'}
function age(ts){if(!ts)return 'never';const s=Math.max(0,Math.floor(Date.now()/1000-ts));if(s<60)return `${s}s ago`;if(s<3600)return `${Math.floor(s/60)}m ago`;if(s<86400)return `${Math.floor(s/3600)}h ago`;return `${Math.floor(s/86400)}d ago`}
async function refresh(){try{const [sr,ar,lr]=await Promise.all([fetch('/api/status'),fetch('/api/activity?limit=60'),fetch('/api/logs')]);const s=await sr.json(),a=await ar.json(),l=await lr.json();document.getElementById('status').textContent=s.bot.status+(s.bot.connected_as?` · u/${s.bot.connected_as}`:'');const d=document.getElementById('dot');d.className='dot '+s.bot.status.replaceAll(' ','-');for(const [k,v] of Object.entries(s.stats)){const e=document.getElementById(k);if(e)e.textContent=Number(v).toLocaleString()}const ks=document.getElementById('keywordstats');const ke=Object.entries(s.keyword_stats||{});ks.innerHTML=ke.length?ke.map(([k,v])=>`<div class=\"event\"><div class=\"eventtop\"><span>${esc(k)}</span><strong>${Number(v).toLocaleString()}</strong></div></div>`).join(''):'<div class=\"small\">No matches yet.</div>';document.getElementById('runtime').textContent=`Uptime: ${fmtDur(s.bot.uptime_seconds)} · Last match: ${age(s.bot.last_match_at)} · Last reply: ${age(s.bot.last_reply_at)}`;const n=document.getElementById('notice');if(!s.bot.credentials_present){n.style.display='block';n.textContent='Reddit credentials are not configured yet. The dashboard still works, but monitoring cannot start.'}else if(s.bot.last_error){n.style.display='block';n.textContent='Last error: '+s.bot.last_error}else n.style.display='none';const box=document.getElementById('activity');box.innerHTML=a.length?a.map(x=>`<div class="event"><div class="eventtop"><span class="kind">${esc(x.kind)}</span><span class="small">${age(x.ts)}</span></div><div>${x.subreddit?`r/${esc(x.subreddit)} `:''}${x.author?`· u/${esc(x.author)}`:''}</div><div class="small">${esc(x.detail||'')}</div>${x.permalink?`<a target="_blank" rel="noreferrer" href="${x.permalink}">Open on Reddit</a>`:''}</div>`).join(''):'<div class="small">No activity yet.</div>';const logs=document.getElementById('logs');logs.textContent=l.lines.join('\n');logs.scrollTop=logs.scrollHeight}catch(e){console.error(e)}}
function esc(s){return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
document.getElementById('save').onclick=async()=>{const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(formConfig())});const j=await r.json();if(!r.ok){toast(j.error||'Save failed');return}fill(j);toast('Saved. Bot restarted with new settings.')}
document.getElementById('preview').onclick=async()=>{const r=await fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(formConfig())});const j=await r.json();document.getElementById('previewbox').textContent=j.reply||j.error}
document.getElementById('reset').onclick=async()=>{if(!confirm('Reset all dashboard counters to zero?'))return;await fetch('/api/stats/reset',{method:'POST'});toast('Counters reset');refresh()}
loadConfig();refresh();setInterval(refresh,3000);
</script></body></html>'''


@app.get("/")
def index():
    return render_template_string(PAGE)


@app.get("/health")
def health():
    return jsonify(ok=True, status=bot.snapshot()["status"])


@app.get("/api/config")
def api_config_get():
    return jsonify(cfg.get())


@app.post("/api/config")
def api_config_save():
    try:
        saved = cfg.save(request.get_json(force=True))
        bot.restart()
        log.info("Settings updated from dashboard")
        return jsonify(saved)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/preview")
def api_preview():
    try:
        temp = deepcopy(DEFAULT_CONFIG)
        temp.update(request.get_json(force=True))
        cfg._validate(temp)
        reply, _ = bot.make_reply(temp)
        return jsonify(reply=reply)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/status")
def api_status():
    return jsonify(bot=bot.snapshot(), stats=store.stats_snapshot(), keyword_stats=store.keyword_stats_snapshot())


@app.get("/api/activity")
def api_activity():
    limit = max(1, min(250, int(request.args.get("limit", 100))))
    return jsonify(store.recent_activity(limit))


@app.get("/api/logs")
def api_logs():
    return jsonify(lines=memory_handler.snapshot(200))


@app.post("/api/stats/reset")
def api_stats_reset():
    store.reset_stats()
    return jsonify(ok=True)


def main():
    log.info("Dashboard starting on port %s", WEB_PORT)
    bot.restart()
    serve(app, host="0.0.0.0", port=WEB_PORT, threads=8)


if __name__ == "__main__":
    main()
