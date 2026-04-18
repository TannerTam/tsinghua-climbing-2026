"""
Tsinghua Climbing 2026 - 赛事排名展示与管理系统

功能:
- 四个赛道(男子预赛/决赛, 女子预赛/决赛), 各自独立排名
- 高度(整数 或 整数+, 带"+"更高) 优先, 同高度按时间升序
- 观众页面只读; 管理员页面可增/改/删选手成绩
- 首次访问强制初始化管理员账号密码; 之后通过登录鉴权

安全措施:
- 密码使用 werkzeug.security 的 pbkdf2 哈希存储 (不保存明文)
- Session 基于 Flask 签名 Cookie, 密钥文件 secret.key 随机生成并放在数据目录
- 登录失败限流 (同 IP 10 次/5 分钟)
- CSRF: 所有写操作使用 POST + session 内 csrf_token 校验
- 输入严格校验, 统一使用参数化 SQL, 避免注入
- 仅监听本机选择的端口, 没有对外暴露任何 shell / 文件接口
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------------------
# 基础配置
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "climbing.db"
SECRET_FILE = DATA_DIR / "secret.key"
CREDENTIALS_FILE = DATA_DIR / "credentials.txt"  # 本地明文凭据备份

BACKUP_DIR = BASE_DIR / "backup"
BACKUP_DIR.mkdir(exist_ok=True)

BACKUP_INTERVAL_SEC = 600  # 每 10 分钟备份一次
BACKUP_KEEP = 100          # 最多保留 100 份 (超出自动删掉最旧)

TRACKS = {
    "men_qual": "男子预赛",
    "men_final": "男子决赛",
    "women_qual": "女子预赛",
    "women_final": "女子决赛",
}

GROUPS = {
    "A": "甲组",
    "B": "乙组",
}

HOST = os.environ.get("TSINGHUA_CLIMBING_HOST", "0.0.0.0")
PORT = int(os.environ.get("TSINGHUA_CLIMBING_PORT", "5000"))


def get_or_create_secret() -> bytes:
    """读取或生成用于签名 session 的密钥, 权限仅限当前用户所在目录。"""
    if SECRET_FILE.exists():
        data = SECRET_FILE.read_bytes()
        if len(data) >= 32:
            return data
    data = secrets.token_bytes(48)
    SECRET_FILE.write_bytes(data)
    return data


app = Flask(__name__, static_folder="static", template_folder="templates")
app.config.update(
    SECRET_KEY=get_or_create_secret(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,  # 8 小时
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,  # 拒绝大请求体
    JSON_AS_ASCII=False,
)


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admins (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    NOT NULL UNIQUE,
                password_hash TEXT    NOT NULL,
                created_at    INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS entries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                track        TEXT    NOT NULL,
                number       TEXT    NOT NULL,
                name         TEXT    NOT NULL,
                height_value INTEGER NOT NULL,
                height_plus  INTEGER NOT NULL DEFAULT 0,  -- 0/1
                time_seconds REAL    NOT NULL,
                group_name   TEXT    NOT NULL DEFAULT 'A',  -- 'A'=甲组, 'B'=乙组
                updated_at   INTEGER NOT NULL,
                UNIQUE (track, number)
            );

            CREATE INDEX IF NOT EXISTS idx_entries_track ON entries(track);
            """
        )
        # 平滑升级: 老数据库如果没有 group_name 列, 追加该列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)").fetchall()}
        if "group_name" not in cols:
            conn.execute(
                "ALTER TABLE entries ADD COLUMN group_name TEXT NOT NULL DEFAULT 'A'"
            )


init_db()


# ---------------------------------------------------------------------------
# 本地备份 (每分钟一次) + 凭据明文保存
# ---------------------------------------------------------------------------


def save_credentials(username: str, password_plain: str) -> None:
    """把管理员账号和明文密码写到本地, 方便你本地查阅找回。

    警告: 该文件包含明文密码, 仅供本机保存。请勿上传到云盘 / git 等。
    """
    content = (
        "# =====================================================\n"
        "#  Tsinghua Climbing 2026 - 管理员凭据 (本机备份)\n"
        "# =====================================================\n"
        "#  警告: 此文件包含你设置的管理员明文密码。\n"
        "#  仅用于本机 找回/查阅, 请勿上传云盘、Git、共享盘等。\n"
        f"#  最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        "# =====================================================\n"
        "\n"
        f"username: {username}\n"
        f"password: {password_plain}\n"
    )
    try:
        CREDENTIALS_FILE.write_text(content, encoding="utf-8")
    except Exception as e:
        print(f"[credentials] 保存失败: {e}")


def _prune_backups(folder: Path, keep: int) -> None:
    """按文件名排序 (时间格式使字典序=时间序), 仅保留最新 keep 份。"""
    files = sorted(folder.glob("climbing-*.db"))
    if keep <= 0:
        return
    for f in files[:-keep]:
        try:
            f.unlink()
        except Exception:
            pass


def do_backup_once() -> None:
    """使用 SQLite 在线备份 API 做一致性快照, 存到 backup/ 以当前时间命名。"""
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d_%H-%M-%S")
    dst = BACKUP_DIR / f"climbing-{ts}.db"
    try:
        src_conn = sqlite3.connect(str(DB_PATH))
        dst_conn = sqlite3.connect(str(dst))
        with dst_conn:
            src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()
    except Exception as e:
        print(f"[backup] 备份失败: {e}")
        return

    _prune_backups(BACKUP_DIR, BACKUP_KEEP)


_backup_started = False
_backup_lock = threading.Lock()


def _backup_loop() -> None:
    time.sleep(5)  # 启动后稍等片刻
    while True:
        try:
            do_backup_once()
        except Exception as e:
            print(f"[backup] 异常: {e}")
        time.sleep(BACKUP_INTERVAL_SEC)


def start_backup_thread() -> None:
    """只启动一次; 守护线程, 进程退出自动结束。"""
    global _backup_started
    with _backup_lock:
        if _backup_started:
            return
        t = threading.Thread(target=_backup_loop, name="backup", daemon=True)
        t.start()
        _backup_started = True


start_backup_thread()


# ---------------------------------------------------------------------------
# 工具: 输入校验
# ---------------------------------------------------------------------------

HEIGHT_RE = re.compile(r"^\s*(\d{1,3})\s*(\+?)\s*$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")


def parse_height(raw: str) -> tuple[int, int]:
    """解析形如 '12' 或 '12+' 的高度, 返回 (整数, 0/1)。"""
    if raw is None:
        raise ValueError("高度不能为空")
    m = HEIGHT_RE.match(str(raw))
    if not m:
        raise ValueError("高度格式错误, 应为整数或整数+, 例如 12 或 12+")
    value = int(m.group(1))
    if value < 0 or value > 999:
        raise ValueError("高度超出范围")
    plus = 1 if m.group(2) == "+" else 0
    return value, plus


def format_height(value: int, plus: int) -> str:
    return f"{value}+" if plus else str(value)


def parse_time(raw: str) -> float:
    """支持 '秒' (123.45) 或 'mm:ss(.ss)' 两种格式。"""
    if raw is None or str(raw).strip() == "":
        raise ValueError("时间不能为空")
    s = str(raw).strip()
    if ":" in s:
        parts = s.split(":")
        if len(parts) != 2:
            raise ValueError("时间格式错误")
        try:
            m = int(parts[0])
            sec = float(parts[1])
        except ValueError:
            raise ValueError("时间格式错误")
        if m < 0 or sec < 0 or sec >= 60:
            raise ValueError("时间格式错误")
        total = m * 60 + sec
    else:
        try:
            total = float(s)
        except ValueError:
            raise ValueError("时间格式错误")
    if total < 0 or total > 60 * 60:
        raise ValueError("时间超出合理范围")
    return round(total, 2)


def format_time(sec: float) -> str:
    if sec >= 60:
        m = int(sec // 60)
        s = sec - m * 60
        return f"{m}:{s:05.2f}"
    return f"{sec:.2f}"


def validate_track(track: str) -> str:
    if track not in TRACKS:
        abort(400, "赛道不存在")
    return track


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


def admin_exists() -> bool:
    row = get_db().execute("SELECT 1 FROM admins LIMIT 1").fetchone()
    return row is not None


def current_admin() -> sqlite3.Row | None:
    uid = session.get("admin_id")
    if not uid:
        return None
    row = get_db().execute("SELECT * FROM admins WHERE id = ?", (uid,)).fetchone()
    return row


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not current_admin():
            if request.method == "GET":
                return redirect(url_for("admin_login", next=request.path))
            return abort(401)
        return view(*a, **kw)

    return wrapper


def get_csrf_token() -> str:
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok


def verify_csrf() -> None:
    sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
    if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
        abort(400, "CSRF 校验失败")


@app.context_processor
def inject_globals():
    return {
        "TRACKS": TRACKS,
        "GROUPS": GROUPS,
        "csrf_token": get_csrf_token,
        "is_admin": bool(current_admin()),
    }


# ---------------- 登录限流 ----------------
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_MAX = 10
_LOGIN_WINDOW = 5 * 60


def _prune(ip: str) -> list[float]:
    now = time.time()
    lst = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _LOGIN_WINDOW]
    _LOGIN_FAILS[ip] = lst
    return lst


def login_rate_limited(ip: str) -> bool:
    return len(_prune(ip)) >= _LOGIN_MAX


def record_login_fail(ip: str) -> None:
    _LOGIN_FAILS.setdefault(ip, []).append(time.time())


# ---------------------------------------------------------------------------
# 排名查询
# ---------------------------------------------------------------------------


def fetch_ranking(track: str, group: str | None = None) -> list[dict]:
    """
    排名规则:
      1) 高度分数越高越靠前; 同高度数字时, 带 '+' 的更高
      2) 高度相同再按时间升序 (时间越短越靠前)

    group 参数:
      - None / "all":  不过滤, 甲乙组混排 (观众页面)
      - "A":           仅甲组, 名次在甲组内部计算
      - "B":           仅乙组, 名次在乙组内部计算
    """
    sql = """
        SELECT id, number, name, height_value, height_plus, time_seconds, group_name
        FROM entries
        WHERE track = ?
    """
    params: list = [track]
    if group in ("A", "B"):
        sql += " AND group_name = ?"
        params.append(group)
    sql += """
        ORDER BY height_value DESC,
                 height_plus  DESC,
                 time_seconds ASC,
                 id ASC
    """
    rows = get_db().execute(sql, params).fetchall()

    result = []
    prev_key: tuple | None = None
    prev_rank = 0
    for idx, r in enumerate(rows, start=1):
        key = (r["height_value"], r["height_plus"], r["time_seconds"])
        rank = prev_rank if key == prev_key else idx
        prev_key, prev_rank = key, rank
        result.append(
            {
                "id": r["id"],
                "rank": rank,
                "number": r["number"],
                "name": r["name"],
                "height": format_height(r["height_value"], r["height_plus"]),
                "height_value": r["height_value"],
                "height_plus": r["height_plus"],
                "time": format_time(r["time_seconds"]),
                "time_seconds": r["time_seconds"],
                "group": r["group_name"],
            }
        )
    return result


# ---------------------------------------------------------------------------
# 路由: 公共
# ---------------------------------------------------------------------------


@app.before_request
def force_setup():
    """没有管理员时, 强制进入首次设置页面。"""
    if admin_exists():
        return
    allowed = {"setup", "static"}
    if request.endpoint in allowed:
        return
    return redirect(url_for("setup"))


@app.route("/")
def index():
    data = {key: fetch_ranking(key) for key in TRACKS}
    return render_template("viewer.html", data=data)


@app.route("/api/rankings")
def api_rankings():
    return jsonify({key: fetch_ranking(key) for key in TRACKS})


# ---------------------------------------------------------------------------
# 路由: 首次设置
# ---------------------------------------------------------------------------


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if admin_exists():
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        verify_csrf()
        username = (request.form.get("username") or "").strip()
        pwd = request.form.get("password") or ""
        pwd2 = request.form.get("password2") or ""
        if not USERNAME_RE.match(username):
            error = "用户名需为 3-32 个字母/数字/下划线/短横线"
        elif len(pwd) < 8:
            error = "密码至少 8 位"
        elif pwd != pwd2:
            error = "两次输入的密码不一致"
        else:
            db = get_db()
            db.execute(
                "INSERT INTO admins(username, password_hash, created_at) VALUES (?,?,?)",
                (username, generate_password_hash(pwd), int(time.time())),
            )
            db.commit()
            save_credentials(username, pwd)
            flash("管理员账号已创建, 请登录。", "ok")
            return redirect(url_for("admin_login"))

    return render_template("setup.html", error=error)


# ---------------------------------------------------------------------------
# 路由: 管理员登录 / 登出
# ---------------------------------------------------------------------------


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if current_admin():
        return redirect(url_for("admin"))

    error = None
    ip = request.remote_addr or "?"

    if request.method == "POST":
        verify_csrf()
        if login_rate_limited(ip):
            error = "登录尝试过于频繁, 请 5 分钟后再试。"
        else:
            username = (request.form.get("username") or "").strip()
            pwd = request.form.get("password") or ""
            row = get_db().execute(
                "SELECT * FROM admins WHERE username = ?", (username,)
            ).fetchone()
            if row and check_password_hash(row["password_hash"], pwd):
                session.clear()
                session["admin_id"] = row["id"]
                session.permanent = True
                get_csrf_token()  # 登录后立即刷新 csrf
                nxt = request.args.get("next") or url_for("admin")
                if not nxt.startswith("/"):
                    nxt = url_for("admin")
                return redirect(nxt)
            record_login_fail(ip)
            error = "账号或密码错误"

    return render_template("login.html", error=error)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    verify_csrf()
    session.clear()
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# 路由: 管理员主页 / 成绩管理
# ---------------------------------------------------------------------------


@app.route("/admin")
@login_required
def admin():
    group_raw = (request.args.get("group") or "all").upper()
    if group_raw in ("A", "B"):
        group_filter: str | None = group_raw
    else:
        group_filter = None
        group_raw = "ALL"
    data = {key: fetch_ranking(key, group_filter) for key in TRACKS}
    return render_template("admin.html", data=data, current_group=group_raw)


@app.route("/admin/entry", methods=["POST"])
@login_required
def admin_entry_upsert():
    verify_csrf()
    entry_id = request.form.get("id", "").strip()
    track = validate_track(request.form.get("track", ""))
    number = (request.form.get("number") or "").strip()
    name = (request.form.get("name") or "").strip()
    height_raw = request.form.get("height") or ""
    time_raw = request.form.get("time") or ""
    group = (request.form.get("group") or "A").strip().upper()
    if group not in GROUPS:
        abort(400, "分组必须是 甲组(A) 或 乙组(B)")

    if not number or len(number) > 16:
        abort(400, "编号长度 1-16")
    if not name or len(name) > 32:
        abort(400, "姓名长度 1-32")

    try:
        h_val, h_plus = parse_height(height_raw)
        t_sec = parse_time(time_raw)
    except ValueError as e:
        abort(400, str(e))

    db = get_db()
    now = int(time.time())

    if entry_id:
        if not entry_id.isdigit():
            abort(400, "id 非法")
        cur = db.execute(
            "SELECT id FROM entries WHERE track = ? AND number = ? AND id != ?",
            (track, number, int(entry_id)),
        ).fetchone()
        if cur:
            abort(400, "该赛道已存在相同编号的选手")
        db.execute(
            """
            UPDATE entries
               SET track=?, number=?, name=?, height_value=?, height_plus=?,
                   time_seconds=?, group_name=?, updated_at=?
             WHERE id=?
            """,
            (track, number, name, h_val, h_plus, t_sec, group, now, int(entry_id)),
        )
    else:
        cur = db.execute(
            "SELECT id FROM entries WHERE track = ? AND number = ?",
            (track, number),
        ).fetchone()
        if cur:
            abort(400, "该赛道已存在相同编号的选手")
        db.execute(
            """
            INSERT INTO entries(track, number, name, height_value, height_plus,
                                time_seconds, group_name, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (track, number, name, h_val, h_plus, t_sec, group, now),
        )
    db.commit()
    # 保留当前筛选, 回到对应赛道
    group_qs = request.form.get("_return_group", "all")
    return redirect(url_for("admin", group=group_qs) + f"#track-{track}")


@app.route("/admin/entry/delete", methods=["POST"])
@login_required
def admin_entry_delete():
    verify_csrf()
    entry_id = request.form.get("id", "").strip()
    if not entry_id.isdigit():
        abort(400, "id 非法")
    db = get_db()
    row = db.execute("SELECT track FROM entries WHERE id=?", (int(entry_id),)).fetchone()
    if not row:
        abort(404)
    db.execute("DELETE FROM entries WHERE id=?", (int(entry_id),))
    db.commit()
    group_qs = request.form.get("_return_group", "all")
    return redirect(url_for("admin", group=group_qs) + f"#track-{row['track']}")


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------


@app.errorhandler(400)
def h400(e):
    return render_template("error.html", code=400, msg=str(e.description)), 400


@app.errorhandler(401)
def h401(e):
    return render_template("error.html", code=401, msg="需要登录"), 401


@app.errorhandler(404)
def h404(e):
    return render_template("error.html", code=404, msg="页面不存在"), 404


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    print("")
    print(f"  监听地址: http://{HOST}:{PORT}")
    print(f"  数据目录: {DATA_DIR}")
    print("")
    print("  首次访问会引导你设置管理员账号和密码。")
    print("  按 Ctrl+C 退出。")
    print("")
    # debug=False 关闭 Werkzeug 调试控制台 (非常重要, 否则存在 RCE 风险)
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
