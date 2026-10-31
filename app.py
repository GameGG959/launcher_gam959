"""
MultiTool Site Server
=====================
Один сервер на всё:
  • сайт (папка web/) и раздача установщика (папка downloads/)
  • аккаунты + скины (Yggdrasil, совместимо с authlib-injector) — под адресом /yggdrasil
  • облако (options.txt, миры, скриншоты)
  • ЛОББИ для мультиплеера (список комнат, коды приглашений, пароль)

Запуск:  python app.py            (порт 8080)
Бой:     gunicorn -w 2 -b 0.0.0.0:8080 app:app   (за HTTPS — Caddy/nginx)
Настройки — переменные окружения, см. .env.example
"""
import base64, hashlib, json, os, random, re, sqlite3, struct, time, uuid
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from flask import Blueprint, Flask, jsonify, request, send_file, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR") or os.path.join(BASE, "data")
WEB = os.path.join(BASE, "web")
DOWNLOADS = os.path.join(BASE, "downloads")
SERVER_NAME = os.environ.get("SERVER_NAME", "MultiTool")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")          # https://mt.example.com
ALLOW_REGISTER = os.environ.get("ALLOW_REGISTER", "1") != "0"
LOBBY_TTL = int(os.environ.get("LOBBY_TTL", "75"))                  # секунд без «пульса» — лобби исчезает
TOKEN_TTL = 30 * 86400
VERSION = "1.4.0"

for d in ("textures", "cloud", "keys", "avatars"):
    os.makedirs(os.path.join(DATA, d), exist_ok=True)
os.makedirs(DOWNLOADS, exist_ok=True)

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 320 * 1024 * 1024
if os.environ.get("BEHIND_PROXY") == "1":          # включай, если стоит Caddy/nginx
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ───────────── ключ подписи текстур ─────────────
KP = os.path.join(DATA, "keys", "private.pem")
if not os.path.exists(KP):
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with open(KP, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
with open(KP, "rb") as f:
    PRIV = serialization.load_pem_private_key(f.read(), None)
PUB = PRIV.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

# ───────────── база ─────────────
def db():
    c = sqlite3.connect(os.path.join(DATA, "db.sqlite3"), timeout=10)
    c.row_factory = sqlite3.Row
    return c

with db() as c:
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,name TEXT UNIQUE COLLATE NOCASE,pw TEXT,skin TEXT,model TEXT DEFAULT 'classic');
    CREATE TABLE IF NOT EXISTS tokens(access TEXT PRIMARY KEY,client TEXT,uid TEXT,ts INTEGER);
    CREATE TABLE IF NOT EXISTS joins(sid TEXT,uid TEXT,ts INTEGER);
    CREATE TABLE IF NOT EXISTS lobbies(id TEXT PRIMARY KEY,owner TEXT UNIQUE,owner_name TEXT,name TEXT,version TEXT,address TEXT,
        max INTEGER,players INTEGER DEFAULT 0,names TEXT DEFAULT '[]',auth TEXT DEFAULT 'site',pw TEXT DEFAULT '',created INTEGER,seen INTEGER);
    """)

    try:
        c.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
    except sqlite3.OperationalError:
        pass

# ───────────── общее ─────────────
@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    r.headers["Access-Control-Expose-Headers"] = "X-Updated, X-Authlib-Injector-API-Location"
    return r

@app.route("/<path:p>", methods=["OPTIONS"])
@app.route("/", methods=["OPTIONS"])
def opt(p=""):
    return "", 204

def err(msg, code=403, kind="ForbiddenOperationException"):
    return jsonify(error=kind, errorMessage=msg), code

HITS = {}
def limited(key, n=10, per=300):
    now = time.time()
    HITS[key] = [t for t in HITS.get(key, []) if now - t < per]
    if len(HITS[key]) >= n:
        return True
    HITS[key].append(now)
    return False

def base_url():
    return PUBLIC_URL or request.host_url.rstrip("/")

def me():
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return None
    with db() as c:
        return c.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.uid WHERE t.access=? AND t.ts>?",
                         (h[7:], int(time.time()) - TOKEN_TTL)).fetchone()

def valid_name(n):
    return 3 <= len(n) <= 16 and n.replace("_", "").isalnum() and n.isascii()

# ───────────── Yggdrasil (authlib-injector) — всё под /yggdrasil ─────────────
ygg = Blueprint("ygg", __name__, url_prefix="/yggdrasil")

def profile(u, signed=True):
    tex = {"timestamp": int(time.time() * 1000), "profileId": u["id"], "profileName": u["name"], "textures": {}}
    if u["skin"]:
        s = {"url": f"{base_url()}/textures/{u['skin']}.png"}
        if u["model"] == "slim":
            s["metadata"] = {"model": "slim"}
        tex["textures"]["SKIN"] = s
    val = base64.b64encode(json.dumps(tex).encode()).decode()
    prop = {"name": "textures", "value": val}
    if signed:
        prop["signature"] = base64.b64encode(PRIV.sign(val.encode(), padding.PKCS1v15(), hashes.SHA1())).decode()
    return {"id": u["id"], "name": u["name"], "properties": [prop]}

@ygg.get("/")
def meta():
    host = urlparse(base_url()).hostname or request.host.split(":")[0]
    return jsonify(meta={"serverName": SERVER_NAME, "implementationName": "multitool-site", "implementationVersion": VERSION,
                         "feature.non_email_login": True, "links": {"homepage": base_url() + "/"}},
                   skinDomains=[host], signaturePublickey=PUB)

@ygg.post("/authserver/authenticate")
def authenticate():
    if limited("auth:" + str(request.remote_addr), 20, 300):
        return err("Слишком много попыток, подожди пару минут", 429)
    d = request.get_json(silent=True) or {}
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE name=?", (str(d.get("username", "")),)).fetchone()
        if not u or not check_password_hash(u["pw"], str(d.get("password", ""))):
            return err("Неверный логин или пароль")
        t = uuid.uuid4().hex
        ct = d.get("clientToken") or uuid.uuid4().hex
        c.execute("INSERT INTO tokens VALUES(?,?,?,?)", (t, ct, u["id"], int(time.time())))
    pr = {"id": u["id"], "name": u["name"]}
    r = {"accessToken": t, "clientToken": ct, "availableProfiles": [pr], "selectedProfile": pr}
    if d.get("requestUser"):
        r["user"] = {"id": u["id"], "properties": []}
    return jsonify(r)

def tok(d):
    with db() as c:
        return c.execute("SELECT * FROM tokens WHERE access=? AND ts>?", (str(d.get("accessToken", "")), int(time.time()) - TOKEN_TTL)).fetchone()

@ygg.post("/authserver/validate")
def validate():
    return ("", 204) if tok(request.get_json(silent=True) or {}) else err("Invalid token")

@ygg.post("/authserver/refresh")
def refresh():
    d = request.get_json(silent=True) or {}
    t = tok(d)
    if not t:
        return err("Invalid token")
    n = uuid.uuid4().hex
    with db() as c:
        c.execute("DELETE FROM tokens WHERE access=?", (t["access"],))
        c.execute("INSERT INTO tokens VALUES(?,?,?,?)", (n, t["client"], t["uid"], int(time.time())))
        u = c.execute("SELECT * FROM users WHERE id=?", (t["uid"],)).fetchone()
    pr = {"id": u["id"], "name": u["name"]}
    return jsonify(accessToken=n, clientToken=t["client"], selectedProfile=pr, availableProfiles=[pr])

@ygg.post("/authserver/invalidate")
def invalidate():
    with db() as c:
        c.execute("DELETE FROM tokens WHERE access=?", (str((request.get_json(silent=True) or {}).get("accessToken", "")),))
    return "", 204

@ygg.post("/authserver/signout")
def signout():
    d = request.get_json(silent=True) or {}
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE name=?", (str(d.get("username", "")),)).fetchone()
        if not u or not check_password_hash(u["pw"], str(d.get("password", ""))):
            return err("Неверный логин или пароль")
        c.execute("DELETE FROM tokens WHERE uid=?", (u["id"],))
    return "", 204

@ygg.post("/sessionserver/session/minecraft/join")
def join():
    d = request.get_json(silent=True) or {}
    t = tok(d)
    if not t or t["uid"] != d.get("selectedProfile"):
        return err("Invalid token")
    with db() as c:
        c.execute("DELETE FROM joins WHERE ts<?", (int(time.time()) - 60,))
        c.execute("INSERT INTO joins VALUES(?,?,?)", (str(d.get("serverId")), t["uid"], int(time.time())))
    return "", 204

@ygg.get("/sessionserver/session/minecraft/hasJoined")
def has_joined():
    with db() as c:
        u = c.execute("SELECT u.* FROM joins j JOIN users u ON u.id=j.uid WHERE j.sid=? AND u.name=? AND j.ts>?",
                      (request.args.get("serverId"), request.args.get("username"), int(time.time()) - 60)).fetchone()
    return jsonify(profile(u)) if u else ("", 204)

@ygg.get("/sessionserver/session/minecraft/profile/<uid>")
def prof(uid):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return jsonify(profile(u, request.args.get("unsigned") == "false")) if u else ("", 204)

@ygg.post("/api/profiles/minecraft")
def by_names():
    names = request.get_json(silent=True) or []
    with db() as c:
        rows = [c.execute("SELECT id,name FROM users WHERE name=?", (str(n),)).fetchone() for n in names[:100]]
    return jsonify([{"id": r["id"], "name": r["name"]} for r in rows if r])

app.register_blueprint(ygg)

# ───────────── аккаунт и скины (REST для сайта и лаунчера) ─────────────
@app.post("/api/register")
def register():
    if not ALLOW_REGISTER:
        return err("Регистрация закрыта", 403)
    if limited("reg:" + str(request.remote_addr), 5, 3600):
        return err("Слишком много регистраций с этого адреса, попробуй позже", 429)
    d = request.get_json(silent=True) or {}
    n, p = str(d.get("username", "")), str(d.get("password", ""))
    if not valid_name(n):
        return err("Логин: 3–16 латинских букв, цифр или _", 400, "BadRequest")
    if len(p) < 6:
        return err("Пароль минимум 6 символов", 400, "BadRequest")
    try:
        with db() as c:
            c.execute("INSERT INTO users(id,name,pw) VALUES(?,?,?)", (uuid.uuid4().hex, n, generate_password_hash(p)))
    except sqlite3.IntegrityError:
        return err("Такой логин уже занят", 409, "Conflict")
    return jsonify(ok=True)

@app.get("/api/me")
def api_me():
    u = me()
    if not u:
        return err("Нужен вход", 401)
    with db() as c:
        lb = c.execute("SELECT id FROM lobbies WHERE owner=? AND seen>?", (u["id"], int(time.time()) - LOBBY_TTL)).fetchone()
    return jsonify(id=u["id"], name=u["name"], model=u["model"], lobby=lb["id"] if lb else None, avatar=f"{base_url()}/avatar/{u['name']}",
                   custom_avatar=bool(u["avatar"]),
                   skin=(f"{base_url()}/textures/{u['skin']}.png" if u["skin"] else None))

@app.put("/api/skin")
def skin():
    u = me()
    if not u:
        return err("Нужен вход", 401)
    b = request.get_data()
    if len(b) > 65536 or b[:8] != b"\x89PNG\r\n\x1a\n":
        return err("Нужен PNG до 64 КБ", 400, "BadRequest")
    w, h = struct.unpack(">II", b[16:24])
    if (w, h) not in ((64, 64), (64, 32)):
        return err("Размер скина 64×64 или 64×32", 400, "BadRequest")
    hs = hashlib.sha256(b).hexdigest()
    with open(os.path.join(DATA, "textures", hs + ".png"), "wb") as f:
        f.write(b)
    with db() as c:
        c.execute("UPDATE users SET skin=?,model=? WHERE id=?", (hs, "slim" if request.args.get("model") == "slim" else "classic", u["id"]))
    return jsonify(ok=True)

@app.delete("/api/skin")
def skin_del():
    u = me()
    if not u:
        return err("Нужен вход", 401)
    with db() as c:
        c.execute("UPDATE users SET skin=NULL WHERE id=?", (u["id"],))
    return jsonify(ok=True)

@app.post("/api/password")
def password():
    u = me()
    if not u:
        return err("Нужен вход", 401)
    d = request.get_json(silent=True) or {}
    if not check_password_hash(u["pw"], str(d.get("old", ""))):
        return err("Старый пароль неверный", 403)
    if len(str(d.get("new", ""))) < 6:
        return err("Пароль минимум 6 символов", 400, "BadRequest")
    keep = request.headers.get("Authorization", "")[7:]
    with db() as c:
        c.execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(str(d["new"])), u["id"]))
        c.execute("DELETE FROM tokens WHERE uid=? AND access<>?", (u["id"], keep))
    return jsonify(ok=True)


# ───────────── аватарки ─────────────
MAGIC = ((b"\x89PNG\r\n\x1a\n", "png", "image/png"), (b"\xff\xd8\xff", "jpg", "image/jpeg"), (b"RIFF", "webp", "image/webp"))

def sniff(b):
    for m, ext, mime in MAGIC:
        if b.startswith(m) and (ext != "webp" or b[8:12] == b"WEBP"):
            return ext, mime
    return None

def default_avatar(name):
    """Красивая круглая аватарка-заглушка: градиент от ника + первая буква."""
    h = int(hashlib.sha256(name.lower().encode()).hexdigest()[:6], 16) % 360
    ch = (name[:1] or "?").upper()
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128"><defs>'
           f'<linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl({h},75%,52%)"/><stop offset="1" stop-color="hsl({(h + 55) % 360},70%,30%)"/></linearGradient>'
           f'<radialGradient id="s" cx=".3" cy=".25" r=".8"><stop offset="0" stop-color="#fff" stop-opacity=".35"/><stop offset=".6" stop-color="#fff" stop-opacity="0"/></radialGradient></defs>'
           f'<rect width="128" height="128" fill="url(#g)"/><rect width="128" height="128" fill="url(#s)"/>'
           f'<text x="64" y="64" text-anchor="middle" dominant-baseline="central" font-family="Segoe UI,Arial,sans-serif" font-weight="700" font-size="64" fill="#fff">{ch}</text></svg>')
    return svg

@app.get("/avatar/<name>")
def avatar(name):
    with db() as c:
        u = c.execute("SELECT name,avatar FROM users WHERE name=?", (name,)).fetchone()
    if u and u["avatar"]:
        p = os.path.join(DATA, "avatars", os.path.basename(u["avatar"]))
        if os.path.exists(p):
            r = send_file(p, mimetype=sniff(open(p, "rb").read(16))[1])
            r.headers["Cache-Control"] = "no-cache"
            return r
    r = app.response_class(default_avatar(u["name"] if u else name[:16]), mimetype="image/svg+xml")
    r.headers["Cache-Control"] = "no-cache"
    return r

@app.put("/api/avatar")
def avatar_put():
    u = me()
    if not u:
        return err("Нужен вход", 401)
    b = request.get_data()
    if len(b) > 262144:
        return err("Картинка больше 256 КБ", 400, "BadRequest")
    t = sniff(b)
    if not t:
        return err("Нужен PNG, JPG или WebP", 400, "BadRequest")
    fn = hashlib.sha256(b).hexdigest()[:32] + "." + t[0]
    with open(os.path.join(DATA, "avatars", fn), "wb") as f:
        f.write(b)
    with db() as c:
        c.execute("UPDATE users SET avatar=? WHERE id=?", (fn, u["id"]))
    return jsonify(ok=True)

@app.delete("/api/avatar")
def avatar_del():
    u = me()
    if not u:
        return err("Нужен вход", 401)
    with db() as c:
        c.execute("UPDATE users SET avatar=NULL WHERE id=?", (u["id"],))
    return jsonify(ok=True)

@app.get("/textures/<h>.png")
def tex(h):
    p = os.path.join(DATA, "textures", os.path.basename(h) + ".png")
    return send_file(p, mimetype="image/png", max_age=3600) if os.path.exists(p) else ("", 404)

# ───────────── облако ─────────────
LIM = {"options": 1 << 20, "screenshots": 100 << 20, "saves": 300 << 20}

@app.route("/api/cloud/<k>", methods=["GET", "PUT"])
def cloud(k):
    u = me()
    if not u:
        return err("Нужен вход", 401)
    if k not in LIM:
        return err("Неизвестный раздел", 404, "NotFound")
    p = os.path.join(DATA, "cloud", u["id"], k + ".bin")
    if request.method == "PUT":
        b = request.get_data()
        if len(b) > LIM[k]:
            return err("Слишком большой файл", 413, "TooLarge")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b)
        return jsonify(ok=True)
    if not os.path.exists(p):
        return ("", 404)
    r = send_file(p, mimetype="application/octet-stream")
    r.headers["X-Updated"] = str(int(os.path.getmtime(p)))
    return r

# ───────────── ЛОББИ ─────────────
ADDR = re.compile(r"^[A-Za-z0-9.\-]{1,100}:\d{2,5}$")
CODE_CH = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # без похожих символов (0/O, 1/I)

def new_code(c):
    while True:
        code = "".join(random.choice(CODE_CH) for _ in range(6))
        if not c.execute("SELECT 1 FROM lobbies WHERE id=?", (code,)).fetchone():
            return code

def live(c):
    c.execute("DELETE FROM lobbies WHERE seen<?", (int(time.time()) - LOBBY_TTL,))

def lobby_pub(r):
    return {"id": r["id"], "name": r["name"], "owner": r["owner_name"], "version": r["version"], "players": r["players"],
            "max": r["max"], "locked": bool(r["pw"]), "auth": r["auth"], "names": json.loads(r["names"] or "[]"),
            "age": int(time.time()) - r["created"]}

@app.get("/api/lobbies")
def lobbies():
    with db() as c:
        live(c)
        rows = c.execute("SELECT * FROM lobbies ORDER BY players DESC, created DESC LIMIT 100").fetchall()
    return jsonify(list=[lobby_pub(r) for r in rows])

@app.post("/api/lobby")
def lobby_create():
    u = me()
    if not u:
        return err("Войди в аккаунт сайта, чтобы создать лобби", 401)
    d = request.get_json(silent=True) or {}
    addr = str(d.get("address", "")).strip()
    if not ADDR.match(addr):
        return err("Неверный адрес сервера", 400, "BadRequest")
    name = re.sub(r"\s+", " ", str(d.get("name", "")).strip())[:40] or f"Лобби {u['name']}"
    ver = re.sub(r"[^\w.\-]", "", str(d.get("version", "")))[:32]
    mx = max(1, min(int(d.get("max") or 8), 100))
    auth = "nick" if d.get("auth") == "nick" else "site"
    pw = generate_password_hash(str(d["password"])) if d.get("password") else ""
    now = int(time.time())
    with db() as c:
        live(c)
        c.execute("DELETE FROM lobbies WHERE owner=?", (u["id"],))
        code = new_code(c)
        c.execute("INSERT INTO lobbies(id,owner,owner_name,name,version,address,max,players,names,auth,pw,created,seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (code, u["id"], u["name"], name, ver, addr, mx, 0, "[]", auth, pw, now, now))
    return jsonify(id=code)

def own_lobby(c, code, u):
    return c.execute("SELECT * FROM lobbies WHERE id=? AND owner=?", (code.upper(), u["id"]),).fetchone()

@app.post("/api/lobby/<code>/ping")
def lobby_ping(code):
    u = me()
    if not u:
        return err("Нужен вход", 401)
    d = request.get_json(silent=True) or {}
    names = [re.sub(r"[^\w]", "", str(n))[:16] for n in (d.get("names") or [])][:100]
    with db() as c:
        if not own_lobby(c, code, u):
            return err("Лобби закрыто", 404, "NotFound")
        c.execute("UPDATE lobbies SET players=?,names=?,seen=? WHERE id=?", (max(0, int(d.get("players") or len(names))), json.dumps(names), int(time.time()), code.upper()))
    return jsonify(ok=True)

@app.delete("/api/lobby/<code>")
def lobby_close(code):
    u = me()
    if not u:
        return err("Нужен вход", 401)
    with db() as c:
        c.execute("DELETE FROM lobbies WHERE id=? AND owner=?", (code.upper(), u["id"]))
    return jsonify(ok=True)

@app.post("/api/lobby/<code>/join")
def lobby_join(code):
    d = request.get_json(silent=True) or {}
    code = re.sub(r"[^A-Za-z0-9]", "", code).upper()
    if limited(f"lj:{request.remote_addr}:{code}", 12, 300):
        return err("Слишком много попыток, подожди пару минут", 429)
    with db() as c:
        live(c)
        r = c.execute("SELECT * FROM lobbies WHERE id=?", (code,)).fetchone()
    if not r:
        return err("Лобби не найдено или уже закрыто", 404, "NotFound")
    if r["pw"] and not check_password_hash(r["pw"], str(d.get("password", ""))):
        return err("Неверный пароль лобби", 403)
    if r["auth"] == "site" and not me():
        return err("Это лобби только для аккаунтов сайта — войди в аккаунт в лаунчере (вкладка ПРОФИЛИ)", 401)
    if r["players"] >= r["max"]:
        return err("Лобби заполнено", 409, "Conflict")
    return jsonify(address=r["address"], version=r["version"], auth=r["auth"], name=r["name"], owner=r["owner_name"])

# ───────────── сайт ─────────────
@app.get("/api/info")
def info():
    with db() as c:
        live(c)
        n = c.execute("SELECT COUNT(*) n, COALESCE(SUM(players),0) p FROM lobbies").fetchone()
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    dl = sorted(f for f in os.listdir(DOWNLOADS) if f.lower().endswith((".exe", ".hta", ".zip")))
    return jsonify(name=SERVER_NAME, version=VERSION, users=users, lobbies=n["n"], playing=n["p"], yggdrasil=base_url() + "/yggdrasil", downloads=dl)

@app.get("/")
def index():
    r = send_from_directory(WEB, "index.html")
    r.headers["X-Authlib-Injector-API-Location"] = "/yggdrasil/"       # авто-обнаружение для authlib-injector
    return r

@app.get("/download/<path:f>")
def download(f):
    return send_from_directory(DOWNLOADS, f, as_attachment=not f.endswith(".json"))

@app.get("/<path:f>")
def web_files(f):
    return send_from_directory(WEB, f)

if __name__ == "__main__":
    print(f"MultiTool Site Server {VERSION}: http://127.0.0.1:{os.environ.get('PORT', 8080)}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
