#!/usr/bin/env python3
"""
Evaluador Res. 3280 – DUSAKAWI EPSI  v0.5.0
Servidor Flask con autenticación, roles y gestión de prestadores
Persistencia: SQLite local (en servidor Contabo) con fallback JSON
"""
import json, os, datetime, uuid, hashlib, sqlite3
from pathlib import Path
from functools import wraps
from flask import (Flask, request, jsonify, render_template, send_file,
                   send_from_directory, session, redirect, url_for)
from werkzeug.utils import secure_filename

from evaluator import RIPSEvaluator

# ── Config ─────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config" / "res3280_cups.json"

# Vercel tiene sistema de archivos read-only; usar /tmp para escritura
_IS_VERCEL = os.environ.get("VERCEL") == "1"
_TMP       = Path("/tmp") if _IS_VERCEL else BASE_DIR
UPLOAD_DIR = _TMP / "uploads"
DATA_PATH  = _TMP / "data"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DATA_PATH.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.json.sort_keys = False  # preservar orden de actividades según config
app.secret_key = os.environ.get("SECRET_KEY", "dusakawi_3280_secret_2026")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

# ── Almacén en memoria ─────────────────────────────────────────────────────
_sessions = {}

# ── SQLite ──────────────────────────────────────────────────────────────────
# Ruta de la base de datos: variable de entorno o carpeta data/ junto al app
_DB_PATH = Path(os.environ.get("SQLITE_DB", str(BASE_DIR.parent / "data" / "evaluador.db")))

def _get_db():
    """Retorna conexión SQLite en autocommit, o None en Vercel."""
    if _IS_VERCEL:
        return None
    try:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_DB_PATH), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _init_db(conn)
        return conn
    except Exception as e:
        import traceback; traceback.print_exc()
        return None

def _migrate_db(conn):
    """Agrega columnas faltantes sin perder datos existentes."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(prestadores)").fetchall()}
    for col, defn in [
        ("departamento",    "TEXT DEFAULT ''"),
        ("rep_legal",       "TEXT DEFAULT ''"),
        ("vigencia_inicio", "TEXT DEFAULT ''"),
        ("vigencia_fin",    "TEXT DEFAULT ''"),
        ("tipo_contrato",   "TEXT DEFAULT 'ASISTENCIAL'"),
        ("lma",             "TEXT DEFAULT '{}'"),
        ("metas",           "TEXT DEFAULT '{}'"),
        ("creado_por",      "TEXT DEFAULT ''"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE prestadores ADD COLUMN {col} {defn}")

def _init_db(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS usuarios (
        id TEXT PRIMARY KEY,
        nombre TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        rol TEXT NOT NULL DEFAULT 'evaluador',
        activo INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS prestadores (
        id TEXT PRIMARY KEY,
        nombre TEXT NOT NULL,
        nit TEXT,
        num_contrato TEXT,
        regimen TEXT,
        departamento TEXT,
        municipio TEXT,
        rep_legal TEXT,
        num_actas INTEGER DEFAULT 0,
        activo INTEGER DEFAULT 1,
        creado_por TEXT,
        vigencia_inicio TEXT,
        vigencia_fin TEXT,
        tipo_contrato TEXT DEFAULT 'ASISTENCIAL',
        lma TEXT DEFAULT '{}',
        metas TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS actas (
        id TEXT PRIMARY KEY,
        prestador_id TEXT,
        periodo TEXT,
        fecha TEXT,
        total_exigido REAL DEFAULT 0,
        total_reconocido REAL DEFAULT 0,
        total_descuento REAL DEFAULT 0,
        pct_cumplimiento REAL DEFAULT 0,
        detalle_json TEXT,
        creado_por TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    _migrate_db(conn)

# ── Persistencia JSON (fallback cuando no hay SQLite) ───────────────────────
USERS_FILE  = DATA_PATH / "users.json"
IPS_FILE    = DATA_PATH / "ips.json"
ACTAS_FILE  = DATA_PATH / "actas.json"

def _hash(pwd): return hashlib.sha256(pwd.encode()).hexdigest()

def _load_users():
    db = _get_db()
    if db:
        try:
            rows = db.execute("SELECT * FROM usuarios ORDER BY created_at").fetchall()
            db.close()
            if rows:
                return [{"id": r["id"], "nombre": r["nombre"], "username": r["username"],
                         "password": r["password_hash"], "rol": r["rol"],
                         "activo": bool(r["activo"])} for r in rows]
        except Exception:
            try: db.close()
            except: pass
    if USERS_FILE.exists():
        with open(USERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    default = [
        {"id": "1", "nombre": "Administrador", "username": "admin",
         "password": _hash("admin123"), "rol": "admin", "activo": True},
        {"id": "2", "nombre": "Jesus Vanegas", "username": "jvanegas",
         "password": _hash("dusakawi2026"), "rol": "evaluador", "activo": True},
    ]
    _save_users(default)
    return default

def _save_users(users):
    db = _get_db()
    if db:
        try:
            for u in users:
                db.execute("""INSERT INTO usuarios (id,nombre,username,password_hash,rol,activo)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        nombre=excluded.nombre, username=excluded.username,
                        password_hash=excluded.password_hash, rol=excluded.rol,
                        activo=excluded.activo""",
                    (u["id"], u["nombre"], u["username"], u["password"],
                     u["rol"], 1 if u.get("activo", True) else 0))
            db.commit()
            db.close()
            return
        except Exception:
            try: db.close()
            except: pass
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

def _load_ips():
    db = _get_db()
    if db:
        try:
            rows = db.execute("SELECT * FROM prestadores ORDER BY created_at").fetchall()
            db.close()
            if rows is not None:
                result = []
                for r in rows:
                    metas = json.loads(r["metas"] or "{}")
                    lma   = json.loads(r["lma"] or "{}")
                    result.append({
                        "id": r["id"], "nombre": r["nombre"], "nit": r["nit"] or "",
                        "num_contrato": r["num_contrato"] or "", "regimen": r["regimen"] or "",
                        "departamento": r["departamento"] or "", "municipio": r["municipio"] or "",
                        "rep_legal": r["rep_legal"] or "", "num_actas": r["num_actas"] or 0,
                        "activo": bool(r["activo"]), "creado_por": r["creado_por"] or "",
                        "vigencia_inicio": r["vigencia_inicio"] or "",
                        "vigencia_fin": r["vigencia_fin"] or "",
                        "tipo_contrato": r["tipo_contrato"] or "ASISTENCIAL",
                        "lma": lma, "metas": metas,
                    })
                return result
        except Exception:
            try: db.close()
            except: pass
    if IPS_FILE.exists():
        with open(IPS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []

def _save_ips(ips):
    db = _get_db()
    if db:
        try:
            for p in ips:
                db.execute("""INSERT INTO prestadores
                    (id,nombre,nit,num_contrato,regimen,departamento,municipio,rep_legal,
                     num_actas,activo,creado_por,vigencia_inicio,vigencia_fin,tipo_contrato,lma,metas)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        nombre=excluded.nombre, nit=excluded.nit,
                        num_contrato=excluded.num_contrato, regimen=excluded.regimen,
                        departamento=excluded.departamento, municipio=excluded.municipio,
                        rep_legal=excluded.rep_legal, num_actas=excluded.num_actas,
                        activo=excluded.activo, creado_por=excluded.creado_por,
                        vigencia_inicio=excluded.vigencia_inicio, vigencia_fin=excluded.vigencia_fin,
                        tipo_contrato=excluded.tipo_contrato, lma=excluded.lma, metas=excluded.metas""",
                    (p["id"], p["nombre"], p.get("nit",""), p.get("num_contrato",""),
                     p.get("regimen",""), p.get("departamento",""), p.get("municipio",""),
                     p.get("rep_legal",""), p.get("num_actas",0),
                     1 if p.get("activo", True) else 0, p.get("creado_por",""),
                     p.get("vigencia_inicio",""), p.get("vigencia_fin",""),
                     p.get("tipo_contrato","ASISTENCIAL"),
                     json.dumps(p.get("lma",{}), ensure_ascii=False),
                     json.dumps(p.get("metas",{}), ensure_ascii=False)))
            db.commit()
            db.close()
            return
        except Exception as e:
            import traceback; traceback.print_exc()
            try: db.close()
            except: pass
    with open(IPS_FILE, "w", encoding="utf-8") as f:
        json.dump(ips, f, ensure_ascii=False, indent=2)

def _load_actas():
    db = _get_db()
    if db:
        try:
            rows = db.execute("SELECT * FROM actas ORDER BY created_at DESC").fetchall()
            db.close()
            if rows is not None:
                return [{
                    "id": r["id"], "prestador_id": r["prestador_id"],
                    "periodo": r["periodo"] or "", "fecha": r["fecha"] or "",
                    "total_exigido": float(r["total_exigido"] or 0),
                    "total_reconocido": float(r["total_reconocido"] or 0),
                    "total_descuento": float(r["total_descuento"] or 0),
                    "pct": float(r["pct_cumplimiento"] or 0),
                    "detalle": json.loads(r["detalle_json"] or "null"),
                    "creado_por": r["creado_por"] or ""
                } for r in rows]
        except Exception:
            try: db.close()
            except: pass
    if ACTAS_FILE.exists():
        with open(ACTAS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []

def _save_actas(actas):
    db = _get_db()
    if db:
        try:
            for a in actas:
                db.execute("""INSERT INTO actas
                    (id,prestador_id,periodo,fecha,total_exigido,total_reconocido,
                     total_descuento,pct_cumplimiento,detalle_json,creado_por)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        prestador_id=excluded.prestador_id, periodo=excluded.periodo,
                        fecha=excluded.fecha, total_exigido=excluded.total_exigido,
                        total_reconocido=excluded.total_reconocido,
                        total_descuento=excluded.total_descuento,
                        pct_cumplimiento=excluded.pct_cumplimiento,
                        detalle_json=excluded.detalle_json, creado_por=excluded.creado_por""",
                    (a["id"], a.get("prestador_id"), a.get("periodo",""), a.get("fecha",""),
                     a.get("total_exigido",0), a.get("total_reconocido",0),
                     a.get("total_descuento",0), a.get("pct",0),
                     json.dumps(a.get("detalle"), ensure_ascii=False),
                     a.get("creado_por","")))
            db.commit()
            db.close()
            return
        except Exception:
            try: db.close()
            except: pass
    with open(ACTAS_FILE, "w", encoding="utf-8") as f:
        json.dump(actas, f, ensure_ascii=False, indent=2)

def _extraer_meta_val(valor) -> float:
    """Extrae el valor numérico de meta desde un dict {meta, upc} o un número."""
    if isinstance(valor, dict):
        return float(valor.get("meta", valor.get("meta_upc", 0)) or 0)
    try:
        return float(valor) if valor else 0.0
    except (TypeError, ValueError):
        return 0.0

def _guardar_metas_supabase(prestador_id: str, metas: dict):
    """Stub de compatibilidad — metas se guardan inline en prestadores SQLite."""
    pass

def _cargar_metas_supabase(prestador_id: str) -> dict:
    """Stub de compatibilidad — metas se leen inline desde prestadores SQLite."""
    return {}

# ── Auth helpers ───────────────────────────────────────────────────────────
class SimpleUser:
    def __init__(self, data):
        self.id       = data["id"]
        self.nombre   = data["nombre"]
        self.username = data["username"]
        self.rol      = data["rol"]
        self.activo   = data.get("activo", True)

def _get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    users = _load_users()
    uid_s = str(uid)
    u = next((u for u in users if str(u["id"]) == uid_s), None)
    return SimpleUser(u) if u else None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = _get_current_user()
            if not user or user.rol not in roles:
                return jsonify({"error": "Sin permisos"}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

# ── Sesión evaluador ───────────────────────────────────────────────────────
def _get_session_dir() -> Path:
    sid = session.get("sid")
    if not sid:
        sid = str(uuid.uuid4())
        session["sid"] = sid
    d = UPLOAD_DIR / sid
    d.mkdir(exist_ok=True)
    return d

def _session_data() -> dict:
    sid = session.get("sid", "")
    if sid not in _sessions:
        _sessions[sid] = {"archivos": {}, "metas": {}, "info_acta": {}, "resultados": None}
    return _sessions[sid]

# ══════════════════════════════════════════════════════════════════════════
# RUTAS AUTH
# ══════════════════════════════════════════════════════════════════════════
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if session.get("user_id"):
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        users = _load_users()
        user = next((u for u in users if u["username"] == username and u.get("activo", True)), None)
        if user and user["password"] == _hash(password):
            session["user_id"] = str(user["id"])
            session.permanent = True
            return redirect(url_for("index"))
        return render_template("login.html", error="Usuario o contraseña incorrectos")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ══════════════════════════════════════════════════════════════════════════
# RUTAS PRINCIPALES
# ══════════════════════════════════════════════════════════════════════════
@app.route("/")
@login_required
def index():
    user = _get_current_user()
    if user is None:
        session.clear()
        return redirect(url_for("login_page"))
    server_label = os.environ.get("SERVER_LABEL", "")
    return render_template("index.html", current_user=user, server_label=server_label)

@app.route("/api/db-status")
@login_required
def api_db_status():
    if _IS_VERCEL:
        return jsonify({"ok": True})
    db = _get_db()
    if db:
        try:
            db.execute("SELECT 1").fetchone()
            db.close()
            return jsonify({"ok": True})
        except Exception:
            try: db.close()
            except: pass
    return jsonify({"ok": False})

@app.route("/api/config")
@login_required
def api_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    return jsonify({"programas": cfg["programas"], "actividades_base": cfg["actividades_base"],
                    "cursos_de_vida": cfg["cursos_de_vida"], "finalidades": cfg.get("finalidades", {}),
                    "rutas_diag": cfg.get("rutas_diag", {})})

# ══════════════════════════════════════════════════════════════════════════
@app.route("/api/debug-db")
def debug_db():
    import traceback as _tb
    result = {"db_path": str(_DB_PATH), "is_vercel": _IS_VERCEL, "db_exists": _DB_PATH.exists()}
    try:
        db = _get_db()
        if db:
            cnt = db.execute("SELECT COUNT(*) FROM prestadores").fetchone()[0]
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            # Test INSERT
            test_id = "debug-test-001"
            try:
                db.execute("""INSERT INTO prestadores
                    (id,nombre,nit,num_contrato,regimen,departamento,municipio,rep_legal,
                     num_actas,activo,creado_por,vigencia_inicio,vigencia_fin,tipo_contrato,lma,metas)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET nombre=excluded.nombre""",
                    (test_id,"TEST","","","SUBSIDIADO","","","",0,1,"debug","","","ASISTENCIAL","{}","{}"))
                cnt2 = db.execute("SELECT COUNT(*) FROM prestadores").fetchone()[0]
                db.execute("DELETE FROM prestadores WHERE id=?", (test_id,))
                result["insert_test"] = "OK"
                result["count_after_insert"] = cnt2
            except Exception as e2:
                result["insert_test"] = "FAIL"
                result["insert_error"] = str(e2)
                result["insert_trace"] = _tb.format_exc()
            db.close()
            result.update({"db_ok": True, "prestadores_count": cnt, "tables": tables})
        else:
            result["db_ok"] = False
    except Exception as e:
        result["db_ok"] = False
        result["error"] = str(e)
        result["trace"] = _tb.format_exc()
    return jsonify(result)

# RUTAS PRESTADORES (IPS)
# ══════════════════════════════════════════════════════════════════════════
@app.route("/api/ips", methods=["GET"])
@login_required
def get_ips():
    return jsonify({"ips": _load_ips()})

@app.route("/api/ips", methods=["POST"])
@login_required
def create_ips():
    user = _get_current_user()
    if user.rol not in ["admin", "evaluador"]:
        return jsonify({"error": "Sin permisos"}), 403
    body = request.get_json() or {}
    if not body.get("nombre"):
        return jsonify({"error": "El nombre es requerido"}), 400
    ips_list = _load_ips()
    new_ips = {
        "id": str(uuid.uuid4()),
        "nombre": body.get("nombre","").upper(),
        "nit": body.get("nit",""),
        "departamento": body.get("departamento",""),
        "municipio": body.get("municipio",""),
        "num_contrato": body.get("num_contrato",""),
        "vigencia_inicio": body.get("vigencia_inicio",""),
        "vigencia_fin": body.get("vigencia_fin",""),
        "rep_legal": body.get("rep_legal",""),
        "regimen": body.get("regimen","SUBSIDIADO"),
        "tipo_contrato": body.get("tipo_contrato","ASISTENCIAL"),
        "lma": body.get("lma", {}),
        "metas": body.get("metas", {}),
        "num_actas": 0,
        "creado_por": user.username,
        "creado_en": datetime.datetime.now().isoformat()
    }
    ips_list.append(new_ips)
    _save_ips(ips_list)
    if new_ips.get("metas"):
        _guardar_metas_supabase(new_ips["id"], new_ips["metas"])
    return jsonify({"ok": True, "ips": new_ips})

@app.route("/api/ips/<ips_id>", methods=["PUT"])
@login_required
def update_ips(ips_id):
    user = _get_current_user()
    if user.rol not in ["admin", "evaluador"]:
        return jsonify({"error": "Sin permisos"}), 403
    body = request.get_json() or {}
    ips_list = _load_ips()
    for ips in ips_list:
        if ips["id"] == ips_id:
            for k in ["nombre","nit","departamento","municipio","num_contrato","vigencia_inicio","vigencia_fin","rep_legal","regimen","tipo_contrato","lma","metas"]:
                if k in body: ips[k] = body[k]
            _save_ips(ips_list)
            if "metas" in body and body["metas"]:
                _guardar_metas_supabase(ips_id, body["metas"])
            return jsonify({"ok": True})
    return jsonify({"error": "No encontrado"}), 404

@app.route("/api/ips/<ips_id>", methods=["DELETE"])
@login_required
def delete_ips(ips_id):
    user = _get_current_user()
    if user.rol not in ["admin", "evaluador"]:
        return jsonify({"error": "Sin permisos"}), 403
    sb = _get_sb()
    if sb:
        try:
            sb.table("metas").delete().eq("prestador_id", ips_id).execute()
            sb.table("prestadores").delete().eq("id", ips_id).execute()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    # Fallback local
    ips_list = _load_ips()
    nueva = [ip for ip in ips_list if ip["id"] != ips_id]
    if len(nueva) == len(ips_list):
        return jsonify({"error": "No encontrado"}), 404
    _save_ips(nueva)
    return jsonify({"ok": True})


@app.route("/api/ips/<ips_id>/metas", methods=["POST"])
@login_required
def set_metas_ips(ips_id):
    """Guarda metas asociadas a una IPS."""
    user = _get_current_user()
    if user.rol not in ["admin", "evaluador"]:
        return jsonify({"error": "Sin permisos"}), 403
    ips_list = _load_ips()
    target = next((ip for ip in ips_list if ip["id"] == ips_id), None)
    if not target:
        return jsonify({"error": "IPS no encontrada"}), 404

    # Carga desde archivo (nota técnica)
    if request.files.get("archivo"):
        f = request.files["archivo"]
        fname = f.filename.lower()
        tmp = UPLOAD_DIR / f"nt_{ips_id}_{f.filename}"
        f.save(str(tmp))
        if fname.endswith(".xlsx") or fname.endswith(".xls"):
            metas = RIPSEvaluator.parsear_nota_tecnica(str(tmp))
        else:
            return jsonify({"error": "Formato no soportado para nota técnica"}), 400
    else:
        body = request.get_json() or {}
        metas = body.get("metas", {})

    target["metas"] = metas
    _save_ips(ips_list)
    _guardar_metas_supabase(ips_id, metas)
    resumen = {prog: sum(acts.values()) if isinstance(acts, dict) else 0 for prog, acts in metas.items()}
    return jsonify({"ok": True, "metas": resumen})


@app.route("/api/extract-metas-preview", methods=["POST"])
@login_required
def extract_metas_preview():
    """
    Recibe un archivo (PDF o Excel de nota técnica) y devuelve:
    - filas: [{cups, descripcion, meta_mes, grupo}]
    - paginas_b64: [str base64 PNG] (solo PDF escaneado)
    """
    f = request.files.get("archivo")
    if not f:
        return jsonify({"error": "Sin archivo"}), 400
    fname = (f.filename or "").lower()
    tmp = UPLOAD_DIR / f"preview_{uuid.uuid4().hex}_{f.filename}"
    f.save(str(tmp))
    try:
        filas = []
        paginas_b64 = []

        if fname.endswith(".pdf"):
            # Intentar extracción de texto primero
            try:
                import fitz  # PyMuPDF
                doc = fitz.open(str(tmp))
                texto_total = "\n".join(page.get_text() for page in doc)
                if len(texto_total.strip()) < 100:
                    # PDF escaneado: renderizar páginas como imágenes
                    import base64, io
                    for page in doc:
                        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                        png_bytes = pix.tobytes("png")
                        paginas_b64.append(base64.b64encode(png_bytes).decode())
                    # Detectar si es nota técnica DI por nombre de archivo
                    es_di = any(k in fname for k in ["anexo 12", "di", "demanda inducida", "sub actividades", "nota tecnica"])
                    if es_di:
                        filas = _plantilla_di()
                else:
                    # PDF con texto: intentar extraer tabla META MES
                    filas = _extraer_filas_texto(texto_total)
            except Exception:
                pass

        elif fname.endswith((".xlsx", ".xls")):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(str(tmp), data_only=True)
                filas = _extraer_filas_excel(wb)
            except Exception as e:
                return jsonify({"error": f"Error leyendo Excel: {e}"}), 400

        return jsonify({"ok": True, "filas": filas, "paginas_b64": paginas_b64})
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def _plantilla_di() -> list:
    """Plantilla estándar de actividades Demanda Inducida Res. 3280 con meta_mes=0."""
    return [
        {"cups": "DI0001-1", "descripcion": "REMISIÓN A VALORACIÓN INTEGRAL PRIMERA VEZ O SEGUIMIENTO (PRIMERA INFANCIA)", "meta_mes": 0, "grupo": "PRIMERA INFANCIA"},
        {"cups": "DI0001-4", "descripcion": "REMISIÓN A ACTUALIZACIÓN DE ESQUEMA PAI (PRIMERA INFANCIA)", "meta_mes": 0, "grupo": "PRIMERA INFANCIA"},
        {"cups": "DI0001-5", "descripcion": "SEGUIMIENTO A INASISTENTES PRIMERA INFANCIA", "meta_mes": 0, "grupo": "PRIMERA INFANCIA"},
        {"cups": "DI0002-1", "descripcion": "REMISIÓN A VALORACIÓN INTEGRAL PRIMERA VEZ O SEGUIMIENTO (INFANCIA)", "meta_mes": 0, "grupo": "INFANCIA"},
        {"cups": "DI0002-4", "descripcion": "REMISIÓN A ACTUALIZACIÓN DE ESQUEMA PAI (INFANCIA)", "meta_mes": 0, "grupo": "INFANCIA"},
        {"cups": "DI0002-5", "descripcion": "SEGUIMIENTO A INASISTENTES INFANCIA", "meta_mes": 0, "grupo": "INFANCIA"},
        {"cups": "DI0003-1", "descripcion": "REMISIÓN A VALORACIÓN INTEGRAL PRIMERA VEZ O SEGUIMIENTO (ADOLESCENCIA)", "meta_mes": 0, "grupo": "ADOLESCENCIA"},
        {"cups": "DI0003-4", "descripcion": "REMISIÓN A ACTUALIZACIÓN DE ESQUEMA PAI (ADOLESCENCIA)", "meta_mes": 0, "grupo": "ADOLESCENCIA"},
        {"cups": "DI0003-5", "descripcion": "SEGUIMIENTO A INASISTENTES ADOLESCENCIA", "meta_mes": 0, "grupo": "ADOLESCENCIA"},
        {"cups": "DI0004-1", "descripcion": "REMISIÓN A VALORACIÓN INTEGRAL PRIMERA VEZ O SEGUIMIENTO (JUVENTUD)", "meta_mes": 0, "grupo": "JOVENES"},
        {"cups": "DI0004-3", "descripcion": "REMISIÓN A ACTUALIZACIÓN DE ESQUEMA PAI (JUVENTUD)", "meta_mes": 0, "grupo": "JOVENES"},
        {"cups": "DI0004-5", "descripcion": "SEGUIMIENTO A INASISTENTES JUVENTUD", "meta_mes": 0, "grupo": "JOVENES"},
        {"cups": "DI0005-1", "descripcion": "REMISIÓN A VALORACIÓN INTEGRAL PRIMERA VEZ O SEGUIMIENTO (ADULTEZ)", "meta_mes": 0, "grupo": "ADULTEZ"},
        {"cups": "DI0005-3", "descripcion": "REMISIÓN A ACTUALIZACIÓN DE ESQUEMA PAI (ADULTEZ)", "meta_mes": 0, "grupo": "ADULTEZ"},
        {"cups": "DI0005-5", "descripcion": "SEGUIMIENTO A INASISTENTES ADULTEZ", "meta_mes": 0, "grupo": "ADULTEZ"},
        {"cups": "DI0006-1", "descripcion": "REMISIÓN A VALORACIÓN INTEGRAL PRIMERA VEZ O SEGUIMIENTO (VEJEZ)", "meta_mes": 0, "grupo": "VEJEZ"},
        {"cups": "DI0006-3", "descripcion": "REMISIÓN A ACTUALIZACIÓN DE ESQUEMA PAI (VEJEZ)", "meta_mes": 0, "grupo": "VEJEZ"},
        {"cups": "DI0006-5", "descripcion": "SEGUIMIENTO A INASISTENTES VEJEZ", "meta_mes": 0, "grupo": "VEJEZ"},
        {"cups": "DI0007-1", "descripcion": "CANALIZACIÓN TOMA DE CITOLOGÍA CERVICOUTERINA", "meta_mes": 0, "grupo": "TAMIZACIONES"},
        {"cups": "DI0006-10", "descripcion": "CANALIZACIÓN DETECCIÓN TEMPRANA CÁNCER DE COLON (SANGRE OCULTA EN HECES - BIANUAL)", "meta_mes": 0, "grupo": "TAMIZACIONES"},
        {"cups": "DI0006-9", "descripcion": "CANALIZACIÓN DETECCIÓN TEMPRANA CÁNCER DE PRÓSTATA (PSA) - CADA 5 AÑOS", "meta_mes": 0, "grupo": "TAMIZACIONES"},
        {"cups": "DI0007-2", "descripcion": "CANALIZACIÓN TOMA DE MAMOGRAFÍA", "meta_mes": 0, "grupo": "TAMIZACIONES"},
        {"cups": "DI0008-1", "descripcion": "CAPTACIÓN USUARIOS DIAGNOSTICADOS HIPERTENSIÓN ARTERIAL - ESTRATEGIA CONOCE TU RIESGO PESO SALUDABLE", "meta_mes": 0, "grupo": "HTA-DM"},
        {"cups": "DI0008-2", "descripcion": "CAPTACIÓN USUARIOS DIAGNOSTICADOS DIABETES MELLITUS - ESTRATEGIA CONOCE TU RIESGO PESO SALUDABLE", "meta_mes": 0, "grupo": "HTA-DM"},
        {"cups": "DI0008-3", "descripcion": "SEGUIMIENTO PACIENTES INASISTENTES Y/O INADHERENTES CON HIPERTENSIÓN ARTERIAL", "meta_mes": 0, "grupo": "HTA-DM"},
        {"cups": "DI0008-4", "descripcion": "SEGUIMIENTO PACIENTES INASISTENTES Y/O INADHERENTES CON DIABETES MELLITUS", "meta_mes": 0, "grupo": "HTA-DM"},
        {"cups": "DI0009-2", "descripcion": "VISITAS DOMICILIARIAS Y APLICACIÓN DE LA FICHA DE RIESGO", "meta_mes": 0, "grupo": "CARACTERIZACION FAMILIAR"},
        {"cups": "DI0009-4", "descripcion": "IDENTIFICACIÓN USUARIOS RENUENTES A LA RUTA Y/O SERVICIOS DE PROMOCIÓN-PREVENCIÓN", "meta_mes": 0, "grupo": "CARACTERIZACION FAMILIAR"},
        {"cups": "DI00011-1", "descripcion": "CANALIZACIÓN CONSULTA PRECONCEPCIONAL", "meta_mes": 0, "grupo": "MATERNO PERINATAL"},
        {"cups": "DI00011-4", "descripcion": "CANALIZACIÓN CONSULTA DE CONTROL PRENATAL", "meta_mes": 0, "grupo": "MATERNO PERINATAL"},
        {"cups": "DI00011-7", "descripcion": "SEGUIMIENTO A INASISTENTES A RMNP", "meta_mes": 0, "grupo": "MATERNO PERINATAL"},
        {"cups": "I11101", "descripcion": "EDUCACIÓN Y COMUNICACIÓN PARA LA PROMOCIÓN DE LA SALUD MENTAL", "meta_mes": 0, "grupo": "SALUD MENTAL"},
        {"cups": "I11104", "descripcion": "EDUCACIÓN Y COMUNICACIÓN EN SALUD - FORTALECIMIENTO DE FACTORES PROTECTORES FRENTE AL CONSUMO", "meta_mes": 0, "grupo": "SALUD MENTAL"},
        {"cups": "I11107", "descripcion": "EDUCACIÓN Y COMUNICACIÓN PARA LA PREVENCIÓN DE CONDUCTA SUICIDA", "meta_mes": 0, "grupo": "SALUD MENTAL"},
        {"cups": "I11110", "descripcion": "EDUCACIÓN Y COMUNICACIÓN - PREVENCIÓN DE PROBLEMAS Y TRASTORNOS MENTALES (INCLUIDA FORMACIÓN DE PRIMEROS RESPONDIENTES)", "meta_mes": 0, "grupo": "SALUD MENTAL"},
        {"cups": "I11202", "descripcion": "EDUCACIÓN Y COMUNICACIÓN PARA LA PREVENCIÓN DE VIOLENCIAS DE GÉNERO Y VIOLENCIAS SEXUALES", "meta_mes": 0, "grupo": "SALUD MENTAL"},
        {"cups": "I11412", "descripcion": "EDUCACIÓN Y COMUNICACIÓN PARA LA ADOPCIÓN DE ESTILOS DE VIDA SALUDABLE", "meta_mes": 0, "grupo": "SALUD MENTAL"},
    ]


def _extraer_filas_texto(texto: str) -> list:
    """Extrae filas {cups, descripcion, meta_mes, grupo} de texto de PDF DI / RCV."""
    import re
    filas = []
    grupo_actual = ""
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        # Detectar encabezado de grupo
        for patron, gid in [
            (r"PRIMERA INFANCIA", "PRIMERA INFANCIA"),
            (r"INFANCIA \(6", "INFANCIA"),
            (r"ADOLESCENCIA", "ADOLESCENCIA"),
            (r"JUVENTUD|JOVEN", "JOVENES"),
            (r"ADULTEZ", "ADULTEZ"),
            (r"VEJEZ", "VEJEZ"),
            (r"TAMIZACI", "TAMIZACIONES"),
            (r"HTA.*DM|ENFERMEDADES PRECURSORAS", "HTA-DM"),
            (r"CARACTERIZACI.*FAMILIAR", "CARACTERIZACION FAMILIAR"),
            (r"MATERNO|PERINATAL", "MATERNO PERINATAL"),
            (r"SALUD MENTAL", "SALUD MENTAL"),
            (r"RIESGO BAJO", "RCV RIESGO BAJO"),
            (r"RIESGO MODERADO", "RCV RIESGO MODERADO"),
            (r"RIESGO ALTO", "RCV RIESGO ALTO"),
        ]:
            if re.search(patron, linea, re.I):
                grupo_actual = gid
                break
        # Detectar fila con CUPS (DI\d+ o I\d+ o \d{6})
        m = re.match(r"(DI\d{4,5}[-\d]*|I\d{5,6}|\d{6,7})\s+(.+?)\s+(\d+)\s*$", linea)
        if m:
            cups, desc, meta_mes = m.group(1), m.group(2).strip(), int(m.group(3))
            filas.append({"cups": cups, "descripcion": desc, "meta_mes": meta_mes, "grupo": grupo_actual})
    return filas


def _extraer_filas_excel(wb) -> list:
    """Extrae filas de nota técnica Excel buscando columnas META MES / META/MES."""
    import re
    filas = []
    for sheet in wb.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue
        # Buscar fila cabecera con "META MES" o "META/MES"
        header_idx = None
        col_cups = col_desc = col_meta_mes = col_grupo = None
        for i, row in enumerate(rows):
            cells = [str(c or "").upper() for c in row]
            for j, c in enumerate(cells):
                if "META MES" in c or "META/MES" in c:
                    col_meta_mes = j
                if c in ("CUPS", "CÓDIGO", "CODIGO"):
                    col_cups = j
                if "DESCRIPCI" in c or "ACTIVIDAD" in c:
                    col_desc = j
            if col_meta_mes is not None:
                header_idx = i
                break
        if header_idx is None or col_meta_mes is None:
            continue
        grupo_actual = ""
        for row in rows[header_idx + 1:]:
            if all(c is None or str(c).strip() == "" for c in row):
                continue
            # Detectar cambio de grupo
            first = str(row[0] or row[1] if len(row) > 1 else "").upper()
            for patron, gid in [
                ("PRIMERA INFANCIA", "PRIMERA INFANCIA"),
                ("INFANCIA", "INFANCIA"),
                ("ADOLESCENCIA", "ADOLESCENCIA"),
                ("JOVEN", "JOVENES"),
                ("ADULTEZ", "ADULTEZ"),
                ("VEJEZ", "VEJEZ"),
                ("TAMIZACI", "TAMIZACIONES"),
                ("HTA", "HTA-DM"),
                ("CARACTERIZACI", "CARACTERIZACION FAMILIAR"),
                ("MATERNO", "MATERNO PERINATAL"),
                ("SALUD MENTAL", "SALUD MENTAL"),
            ]:
                if patron in first:
                    grupo_actual = gid
                    break
            meta_val = row[col_meta_mes] if col_meta_mes < len(row) else None
            try:
                meta_mes = int(float(str(meta_val))) if meta_val not in (None, "") else None
            except Exception:
                meta_mes = None
            if meta_mes is None:
                continue
            cups = str(row[col_cups]).strip() if col_cups is not None and col_cups < len(row) else ""
            desc = str(row[col_desc]).strip() if col_desc is not None and col_desc < len(row) else ""
            if cups or desc:
                filas.append({"cups": cups, "descripcion": desc, "meta_mes": meta_mes, "grupo": grupo_actual})
    return filas


# ══════════════════════════════════════════════════════════════════════════
# RUTAS USUARIOS
# ══════════════════════════════════════════════════════════════════════════
@app.route("/api/usuarios", methods=["GET"])
@login_required
def get_usuarios():
    user = _get_current_user()
    if user.rol != "admin":
        return jsonify({"error": "Sin permisos"}), 403
    users = _load_users()
    # No devolver password
    safe = [{"id":u["id"],"nombre":u["nombre"],"username":u["username"],
              "rol":u["rol"],"activo":u.get("activo",True)} for u in users]
    return jsonify({"usuarios": safe})

@app.route("/api/usuarios", methods=["POST"])
@login_required
def create_usuario():
    user = _get_current_user()
    if user.rol != "admin":
        return jsonify({"error": "Sin permisos"}), 403
    body = request.get_json() or {}
    if not body.get("username") or not body.get("password"):
        return jsonify({"error": "Usuario y contraseña son requeridos"}), 400
    users = _load_users()
    if any(u["username"] == body["username"] for u in users):
        return jsonify({"error": "El usuario ya existe"}), 400
    new_user = {
        "id": str(uuid.uuid4()),
        "nombre": body.get("nombre", body["username"]),
        "username": body["username"],
        "password": _hash(body["password"]),
        "rol": body.get("rol", "evaluador"),
        "activo": True
    }
    users.append(new_user)
    _save_users(users)
    return jsonify({"ok": True})

# ══════════════════════════════════════════════════════════════════════════
# RUTAS EVALUACIÓN (mismas que antes + auth)
# ══════════════════════════════════════════════════════════════════════════
def _parsear_rips_txt(content_bytes):
    """
    Parsea el formato TXT de RIPS (Res. 3374/2000): pipe-delimited, latin-1.
    Retorna dict {seccion: [lista_de_dicts]}.
    """
    text = content_bytes.decode("latin-1")
    lines = [l.rstrip("|").strip() for l in text.replace("\r","").split("\n")]

    secciones_raw = {}
    seccion_actual = None
    for line in lines:
        if line.startswith("°---- ARCHIVO-") and line.endswith("----°"):
            nombre_sec = line.replace("°---- ARCHIVO-","").replace(" ----°","").strip()
            if nombre_sec not in secciones_raw:
                secciones_raw[nombre_sec] = []
                seccion_actual = nombre_sec
            else:
                seccion_actual = None  # marcador de cierre — dejar de leer esta sección
        elif seccion_actual and line and not line.startswith("°"):
            secciones_raw[seccion_actual].append(line)

    result = {}

    def cols(line):
        return [c.strip() for c in line.split(",")]

    if "USUARIOS" in secciones_raw:
        rows = []
        for line in secciones_raw["USUARIOS"]:
            c = cols(line)
            if len(c) < 5: continue
            rows.append({
                "tipoDocumentoIdentificacion": c[0],
                "numDocumentoIdentificacion":  c[1],
                "codPrestador":                c[2] if len(c) > 2 else "",
                "fechaNacimiento":             c[3] if len(c) > 3 else "",
                "codSexo":                     c[4] if len(c) > 4 else "",
                "codZona":                     c[5] if len(c) > 5 else "",
                "codMunicipio":                c[6] if len(c) > 6 else "",
            })
        result["usuarios"] = rows

    if "CONSULTAS" in secciones_raw:
        rows = []
        for line in secciones_raw["CONSULTAS"]:
            c = cols(line)
            if len(c) < 7: continue
            rows.append({
                "tipoDocumentoIdentificacion":  c[1],
                "numDocumentoIdentificacion":   c[2],
                "fechaInicioAtencion":          c[4][:10] if len(c) > 4 else "",
                "codConsulta":                  c[6] if len(c) > 6 else "",
                "finalidadTecnologiaSalud":     c[20] if len(c) > 20 else "",
            })
        result["consultas"] = rows

    if "PROCEDIMIENTOS" in secciones_raw:
        rows = []
        for line in secciones_raw["PROCEDIMIENTOS"]:
            c = cols(line)
            if len(c) < 8: continue
            rows.append({
                "tipoDocumentoIdentificacion":  c[1],
                "numDocumentoIdentificacion":   c[2],
                "fechaInicioAtencion":          c[4][:10] if len(c) > 4 else "",
                "codProcedimiento":             c[7] if len(c) > 7 else "",
                "finalidadTecnologiaSalud":     c[19] if len(c) > 19 else "",
            })
        result["procedimientos"] = rows

    if "MEDICAMENTOS" in secciones_raw:
        rows = []
        for line in secciones_raw["MEDICAMENTOS"]:
            c = cols(line)
            if len(c) < 10: continue
            rows.append({
                "tipoDocumentoIdentificacion":   c[1],
                "numDocumentoIdentificacion":    c[2],
                "fechaDispensacionMedicamento":  c[6][:10] if len(c) > 6 else "",
                "codTecnologiaSalud":            c[10] if len(c) > 10 else "",
                "nomTecnologiaSalud":            c[11] if len(c) > 11 else "",
            })
        result["medicamentos"] = rows

    if "OTROS SERVICIOS" in secciones_raw:
        rows = []
        for line in secciones_raw["OTROS SERVICIOS"]:
            c = cols(line)
            if len(c) < 8: continue
            rows.append({
                "tipoDocumentoIdentificacion":  c[1],
                "numDocumentoIdentificacion":   c[2],
                "fechaInicioAtencion":          c[6][:10] if len(c) > 6 else "",
                "codServicio":                  c[8] if len(c) > 8 else "",
                "nomServicio":                  c[9] if len(c) > 9 else "",
            })
        result["otrosServicios"] = rows

    return result


@app.route("/api/upload-rips", methods=["POST"])
@login_required
def upload_rips():
    if "files" not in request.files:
        return jsonify({"error": "No se recibieron archivos"}), 400
    sd = _session_data()
    info = []
    for file in request.files.getlist("files"):
        if not file.filename: continue
        fname_lower = file.filename.lower()

        # ── Formato TXT (RIPS Res. 3374 antiguo) ──────────────────────────────
        if fname_lower.endswith(".txt"):
            try:
                content = file.read()
                secciones = _parsear_rips_txt(content)
                if not secciones:
                    info.append({"archivo": file.filename, "error": "No se encontraron secciones RIPS en el archivo", "ok": False})
                    continue
                for seccion, datos in secciones.items():
                    sd["archivos"][seccion] = datos
                    info.append({"archivo": seccion, "registros": len(datos), "ok": True})
            except Exception as e:
                info.append({"archivo": file.filename, "error": str(e), "ok": False})
            continue

        # ── Formato JSON (RIPS nuevo) ─────────────────────────────────────────
        nombre = fname_lower.replace(".json","")
        canon = _canonicalizar_nombre(nombre)
        try:
            data = json.load(file)
            if not isinstance(data, list): data = [data]
            sd["archivos"][canon] = data
            info.append({"archivo": canon, "registros": len(data), "ok": True})
        except Exception as e:
            info.append({"archivo": nombre, "error": str(e), "ok": False})

    # Calcular cobertura y detalle de usuarios usando RIPSEvaluator
    cobertura = {}
    total_usuarios = 0
    usuarios_detalle = []
    try:
        from evaluator import RIPSEvaluator
        ev = RIPSEvaluator()
        for nombre, datos in sd["archivos"].items():
            ev.cargar_archivo(nombre, datos)
        ev._calcular_grupos()
        total_usuarios = len(ev._usuarios)
        for info_u in ev._usuarios.values():
            g = info_u.get("grupo")
            if g: cobertura[g] = cobertura.get(g, 0) + 1

        # Detalle por usuario: actividades (CUPS) y cuántas veces aparece
        ARCH_CUPS = {
            "consultas":      ("codConsulta",        "fechaInicioAtencion"),
            "procedimientos": ("codProcedimiento",   "fechaInicioAtencion"),
            "medicamentos":   ("codTecnologiaSalud", "fechaDispensacionMedicamento"),
            "otrosServicios": ("codTecnologiaSalud", "fechaInicioAtencion"),
        }
        # Índice numDoc → lista de (cups, fecha, archivo)
        actos_por_num: dict = {}
        for arch, (cups_key, fecha_key) in ARCH_CUPS.items():
            for r in ev._archivos.get(arch, []):
                num = str(r.get("numDocumentoIdentificacion","") or "").strip()
                cups = str(r.get(cups_key,"") or "").strip()
                fecha = str(r.get(fecha_key,"") or "")[:10]
                if num and cups:
                    actos_por_num.setdefault(num, []).append({"cups": cups, "fecha": fecha, "archivo": arch})

        for (tipo, num), info_u in ev._usuarios.items():
            actos = actos_por_num.get(num, [])
            # Contar repeticiones por CUPS
            cups_count: dict = {}
            for a in actos:
                cups_count[a["cups"]] = cups_count.get(a["cups"], 0) + 1
            repetidos = {k: v for k, v in cups_count.items() if v > 1}
            usuarios_detalle.append({
                "tipo_doc": tipo, "num_doc": num,
                "edad": info_u.get("edad"), "sexo": info_u.get("sexo"),
                "grupo": info_u.get("grupo"),
                "total_actos": len(actos),
                "cups_repetidos": repetidos,
            })
        usuarios_detalle.sort(key=lambda x: (x.get("grupo") or "", x["num_doc"]))
    except Exception as e:
        cobertura = {"error": str(e)}
    return jsonify({"archivos_cargados": list(sd["archivos"].keys()), "detalle": info,
                    "cobertura_poblacion": cobertura, "total_usuarios": total_usuarios,
                    "usuarios_detalle": usuarios_detalle})

@app.route("/api/procesar-rips", methods=["POST"])
@login_required
def procesar_rips():
    """Recibe datos RIPS pre-parseados por el browser y los guarda en sesión."""
    body = request.get_json(force=True, silent=True) or {}
    detalle_in = body.get("detalle", [])
    cobertura = body.get("cobertura_poblacion", {})
    usuarios_detalle = body.get("usuarios_detalle", [])
    total_usuarios = body.get("total_usuarios", len(usuarios_detalle))

    # Los archivos RIPS se guardan solo en el browser (window._ripsDatos)
    # El servidor guarda el resumen en sesión para la evaluación
    sd = _session_data()
    sd["cobertura"] = cobertura
    sd["usuarios_detalle"] = usuarios_detalle
    sd["total_usuarios"] = total_usuarios

    if not cobertura:
        cobertura = {"error": "No se recibió cobertura del cliente"}

    return jsonify({"archivos_cargados": list(sd["archivos"].keys()),
                    "detalle": detalle_in,
                    "cobertura_poblacion": cobertura,
                    "total_usuarios": total_usuarios,
                    "usuarios_detalle": usuarios_detalle})


def _canonicalizar_nombre(nombre):
    nombre = nombre.lower()
    for alias, canon in [
        ("consultaambulatorio","consultas"),("consultaambulatorias","consultas"),
        ("consulta","consultas"),("procedimiento","procedimientos"),
        ("otroservicio","otrosServicios"),("otrosservicio","otrosServicios"),
        ("otro_servicio","otrosServicios"),("medicamento","medicamentos"),
        ("usuario","usuarios"),
    ]:
        if alias in nombre: return canon
    return nombre

@app.route("/api/upload-nota-tecnica", methods=["POST"])
@login_required
def upload_nota_tecnica():
    if "file" not in request.files:
        return jsonify({"error": "No se recibió archivo"}), 400
    file = request.files["file"]
    if not file.filename.endswith((".xlsx",".xls")):
        return jsonify({"error": "Solo se aceptan archivos Excel (.xlsx)"}), 400
    tmp = _get_session_dir() / secure_filename(file.filename)
    file.save(str(tmp))
    try:
        metas = RIPSEvaluator.parsear_nota_tecnica(str(tmp))
        sd = _session_data()
        sd["metas"] = metas
        resumen = {prog: {act: v["meta"] for act, v in acts.items() if act != "__upc"}
                   for prog, acts in metas.items()}
        return jsonify({"ok": True, "metas": resumen, "programas": list(metas.keys())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/set-metas", methods=["POST"])
@login_required
def set_metas():
    body = request.get_json()
    if not body: return jsonify({"error": "No se recibieron datos"}), 400
    sd = _session_data()
    sd["metas"] = body.get("metas", {})
    sd["info_acta"] = body.get("info_acta", {})
    return jsonify({"ok": True})

@app.route("/api/evaluar", methods=["POST"])
@login_required
def evaluar():
    sd   = _session_data()
    body = request.get_json() or {}
    if "info_acta" in body: sd["info_acta"] = body["info_acta"]
    if "metas" in body: sd["metas"] = body["metas"]
    periodo_str = sd.get("info_acta",{}).get("periodo_fin","")
    periodo_ref = None
    if periodo_str:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try: periodo_ref = datetime.datetime.strptime(periodo_str, fmt).date(); break
            except: pass
    ev = RIPSEvaluator(periodo_ref=periodo_ref)
    for nombre, data in sd["archivos"].items():
        ev.cargar_archivo(nombre, data)
    metas = sd.get("metas", {})
    resultados = ev.evaluar(metas)
    resumen    = ev.resumen_acta(resultados)
    sd["resultados"] = resultados
    sd["resumen"]    = resumen
    total_e = sum(r["total_exigido"]    for r in resultados.values())
    total_r = sum(r["total_reconocido"] for r in resultados.values())
    total_d = sum(r["total_descuento"]  for r in resultados.values())
    return jsonify({"ok": True, "resultados": resultados, "resumen": resumen,
                    "totales": {"exigido": total_e, "reconocido": total_r, "descuento": total_d,
                                "pct": total_r/total_e if total_e else 1.0}})

@app.route("/api/preeval", methods=["POST"])
@login_required
def preeval():
    """Pre-evaluación usando datos pre-agregados del browser o archivos en sesión."""
    sd = _session_data()
    body = request.get_json() or {}
    conteos = body.get("conteos")  # {archivo:{grupo:{cups|finalidad:count}}} del browser

    cfg = RIPSEvaluator().cfg  # solo para acceder a la config, sin datos

    if conteos:
        # ── Modo rápido: usar conteos pre-calculados en el browser ──────────
        resultados = {}
        cobertura = sd.get("cobertura", {})
        total_usuarios = sd.get("total_usuarios", 0)

        cvs_ids = {cv["id"] for cv in cfg.get("cursos_de_vida", [])}
        # Mapa grupo_id → grupos del browser que aplican
        def _grupos_para_prog(prog):
            pid = prog["id"]
            # Si tiene aplica_a explícito, usarlo directamente (DX groups: EMBARAZADA, DM, etc.)
            if "aplica_a" in prog and prog["aplica_a"]:
                return list(prog["aplica_a"])
            if pid in cvs_ids:
                return [pid]  # Curso de vida exacto
            # DI sin aplica_a → todos los cursos de vida cuya edad caiga en rango
            edad_min = prog.get("edad_min", 0)
            edad_max = prog.get("edad_max", 200)
            EDAD_CV = {
                "PRIMERA_INFANCIA": (0, 5), "INFANCIA": (6, 11),
                "ADOLESCENCIA": (12, 17), "JOVENES": (18, 28),
                "ADULTEZ": (29, 59), "VEJEZ": (60, 200)
            }
            return [g for g, (mn, mx) in EDAD_CV.items() if mn <= edad_max and mx >= edad_min]

        for prog in cfg.get("programas", []):
            pid = prog["id"]
            grupos_aplicables = _grupos_para_prog(prog)
            acts = {}
            for aid in prog.get("actividades", []):
                act_cfg = cfg["actividades_base"].get(aid)
                if not act_cfg: continue
                archivo = act_cfg.get("archivo", "")
                cups_list = [str(c).strip().upper() for c in act_cfg.get("cups", [])]
                finalidades = [str(f).strip() for f in act_cfg.get("finalidad", [])]
                nombres_kw = [str(n).strip().upper() for n in act_cfg.get("nombres", [])]
                por_registro = act_cfg.get("por_registro", False)
                max_pac = act_cfg.get("max_por_paciente", {})
                grupo_map = conteos.get(archivo, {})

                encontrados_total = 0
                encontrados_fin = 0

                if nombres_kw and not cups_list:
                    # Medicamentos: matching por nombre (keyword substring)
                    # Para medicamentos siempre es por_registro (cada dispensación cuenta)
                    reg_map = conteos.get("__por_registro", {}).get("medicamentos", {})
                    nombre_map = conteos.get("__med_nombres", {})
                    for grupo in grupos_aplicables:
                        max_n = max_pac.get(grupo, max_pac.get("default", 9999))
                        for nombre_val, count in nombre_map.get(grupo, {}).items():
                            if any(kw in nombre_val for kw in nombres_kw):
                                encontrados_total += count
                                encontrados_fin += count

                elif por_registro:
                    # Contar registros totales por paciente, aplicar max por paciente/grupo
                    reg_map = conteos.get("__por_registro", {}).get(archivo, {})
                    for grupo in grupos_aplicables:
                        max_n = max_pac.get(grupo, max_pac.get("default", 9999))
                        for ckey, pids in reg_map.get(grupo, {}).items():
                            cups_val = ckey.split("|")[0]
                            fin_val = ckey.split("|")[1] if "|" in ckey else ""
                            if cups_val not in cups_list: continue
                            for cnt in pids.values():
                                encontrados_total += min(cnt, max_n)
                            if not finalidades or fin_val in finalidades:
                                for cnt in pids.values():
                                    encontrados_fin += min(cnt, max_n)

                else:
                    # Pacientes únicos por CUPS (sin duplicar por finalidad)
                    cups_only_map = conteos.get("__cups_only", {}).get(archivo, {})
                    for grupo in grupos_aplicables:
                        for cups_val, count in cups_only_map.get(grupo, {}).items():
                            if cups_val in cups_list:
                                encontrados_total += count
                    # Con finalidad: usar índice cups|fin
                    for grupo in grupos_aplicables:
                        for ckey, count in grupo_map.get(grupo, {}).items():
                            cups_val = ckey.split("|")[0]
                            fin_val = ckey.split("|")[1] if "|" in ckey else ""
                            if cups_val not in cups_list: continue
                            if not finalidades or fin_val in finalidades:
                                encontrados_fin += count
                    # Fallback: si el archivo principal dio 0, buscar en archivo_fallback
                    # Replica: =SI(CONTAR.SI(procedimientos!H:H;"CUPS")>0; ...; CONTAR.SI.CONJUNTO(otrosServicios...))
                    if encontrados_total == 0 and act_cfg.get("archivo_fallback"):
                        fb_arch = act_cfg["archivo_fallback"]
                        fb_cups_map = conteos.get("__cups_only", {}).get(fb_arch, {})
                        for grupo in grupos_aplicables:
                            for cups_val, count in fb_cups_map.get(grupo, {}).items():
                                if cups_val in cups_list:
                                    encontrados_total += count
                        encontrados_fin = encontrados_total

                # Audit: rutas especificas — total y con fin, para calcular sinFin por ruta
                ruta_audit = {}
                ruta_sin_fin = {}
                if archivo == "consultas":
                    ruta_audit_raw = conteos.get("__ruta_audit", {})
                    ruta_audit_fin_raw = conteos.get("__ruta_audit_fin", {})
                    for grupo in grupos_aplicables:
                        grp_all = ruta_audit_raw.get(grupo, {})
                        grp_fin = ruta_audit_fin_raw.get(grupo, {})
                        for cups_val in cups_list:
                            for ruta, cnt in grp_all.get(cups_val, {}).items():
                                ruta_audit[ruta] = ruta_audit.get(ruta, 0) + cnt
                        for fin_val in finalidades:
                            ckey = cups_val + "|" + fin_val if finalidades else cups_val
                            for cups_val2 in cups_list:
                                ckey2 = cups_val2 + "|" + fin_val
                                for ruta, cnt in grp_fin.get(ckey2, {}).items():
                                    ruta_sin_fin[ruta] = ruta_sin_fin.get(ruta, 0) - cnt
                    # sinFin = total - con_fin (non-negative)
                    for ruta in list(ruta_sin_fin.keys()):
                        total = ruta_audit.get(ruta, 0)
                        ruta_sin_fin[ruta] = max(0, total + ruta_sin_fin.get(ruta, 0))
                    # Add base from ruta_audit for rutas not yet in ruta_sin_fin
                    for ruta, total in ruta_audit.items():
                        if ruta not in ruta_sin_fin:
                            # No fin data = all are sinFin
                            ruta_sin_fin[ruta] = total
                    ruta_sin_fin = {k: v for k, v in ruta_sin_fin.items() if v > 0}

                acts[aid] = {
                    "descripcion": act_cfg.get("descripcion", aid),
                    "archivo": archivo,
                    "cups": act_cfg.get("cups", []),
                    "grupos": grupos_aplicables,
                    "finalidades": finalidades,
                    "encontrados": encontrados_total,
                    "encontrados_fin": encontrados_fin,
                    "tiene_finalidad": bool(finalidades),
                    "ruta_audit": ruta_audit or None,
                    "ruta_sin_fin": ruta_sin_fin or None,
                }
            if any(v["encontrados"] > 0 for v in acts.values()):
                resultados[pid] = {
                    "nombre": prog.get("nombre", pid),
                    "actividades": acts,
                    "total": sum(v["encontrados"] for v in acts.values()),
                    "total_fin": sum(v["encontrados_fin"] for v in acts.values()),
                }

        return jsonify({"ok": True, "resultados": resultados, "cobertura": cobertura,
                        "archivos": {}, "total_usuarios": total_usuarios})

    # ── Fallback: usar archivos en sesión (caso local/dev) ───────────────
    periodo_str = body.get("periodo_fin", sd.get("info_acta", {}).get("periodo_fin", ""))
    periodo_ref = None
    if periodo_str:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try: periodo_ref = datetime.datetime.strptime(periodo_str, fmt).date(); break
            except: pass

    ev = RIPSEvaluator(periodo_ref=periodo_ref)
    for nombre, data in sd["archivos"].items():
        ev.cargar_archivo(nombre, data)
    ev._calcular_grupos()

    resultados = {}
    for prog in cfg.get("programas", []):
        pid = prog["id"]
        acts = {}
        for aid in prog.get("actividades", []):
            act_cfg = cfg["actividades_base"].get(aid)
            if not act_cfg: continue
            total = ev._contar_actividad(act_cfg, pid)
            acts[aid] = {
                "descripcion": act_cfg.get("descripcion", aid),
                "archivo": act_cfg.get("archivo", ""),
                "cups": act_cfg.get("cups", []),
                "encontrados": total
            }
        if any(v["encontrados"] > 0 for v in acts.values()):
            resultados[pid] = {
                "nombre": prog.get("nombre", pid),
                "actividades": acts,
                "total": sum(v["encontrados"] for v in acts.values())
            }

    cobertura = {}
    for info in ev._usuarios.values():
        g = info.get("grupo")
        if g: cobertura[g] = cobertura.get(g, 0) + 1

    return jsonify({"ok": True, "resultados": resultados, "cobertura": cobertura,
                    "archivos": {k: len(v) for k, v in sd["archivos"].items()},
                    "total_usuarios": len(ev._usuarios)})

@app.route("/api/preeval/exportar", methods=["POST"])
@login_required
def preeval_exportar():
    """Genera archivo Excel con los resultados de la pre-evaluación."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    body = request.get_json() or {}
    resultados = body.get("resultados", {})
    cobertura = body.get("cobertura", {})
    total_usuarios = body.get("total_usuarios", 0)
    prestador_nombre = body.get("prestador_nombre", "")
    fecha = body.get("fecha", datetime.datetime.now().strftime("%Y-%m-%d"))
    usar_fin = body.get("usar_fin", True)
    sumar_sin_fin = set(body.get("sumar_sin_fin", []))

    wb = Workbook()
    ws = wb.active
    ws.title = "Pre-Evaluación"

    # Estilos
    hdr_fill   = PatternFill("solid", fgColor="1E3A8A")
    prog_fill  = PatternFill("solid", fgColor="DBEAFE")
    seg_fill   = PatternFill("solid", fgColor="1E40AF")
    total_fill = PatternFill("solid", fgColor="EFF6FF")
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # Título
    ws.merge_cells("A1:F1")
    ws["A1"] = f"PRE-EVALUACIÓN RIPS — Res. 3280/2018"
    ws["A1"].font = Font(bold=True, size=13, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor="1E3A8A")
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 22

    ws.merge_cells("A2:F2")
    ws["A2"] = f"{prestador_nombre}   |   Fecha: {fecha}   |   Usuarios RIPS: {total_usuarios:,}".replace(",",".")
    ws["A2"].font = Font(italic=True, size=10, color="374151")
    ws["A2"].alignment = Alignment(horizontal="center")

    # Cobertura
    row = 4
    ws.merge_cells(f"A{row}:F{row}")
    ws[f"A{row}"] = "COBERTURA POBLACIONAL"
    ws[f"A{row}"].font = Font(bold=True, size=10, color="FFFFFF")
    ws[f"A{row}"].fill = seg_fill
    ws[f"A{row}"].alignment = Alignment(horizontal="center")
    row += 1
    GRUPOS_LABELS = {"PRIMERA_INFANCIA":"1ra Infancia","INFANCIA":"Infancia",
        "ADOLESCENCIA":"Adolescencia","JOVENES":"Jóvenes","ADULTEZ":"Adultez","VEJEZ":"Vejez",
        "EMBARAZADA":"Embarazadas","PRECONCEPCIONAL":"Preconcepcional","DM":"Diabetes","HIPERTENSION":"Hipertensión"}
    for g, n in cobertura.items():
        ws[f"A{row}"] = GRUPOS_LABELS.get(g, g)
        ws[f"B{row}"] = n
        ws[f"B{row}"].alignment = Alignment(horizontal="center")
        row += 1

    row += 1
    # Cabecera de tabla
    headers = ["Programa / Actividad", "Archivo", "Encontrados", "Sin Finalidad", "Meta", ""]
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=col, value=h)
        c.font = Font(bold=True, size=10, color="FFFFFF")
        c.fill = hdr_fill
        c.alignment = Alignment(horizontal="center")
        c.border = border
    ws.column_dimensions["A"].width = 55
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 14
    ws.column_dimensions["E"].width = 10
    row += 1

    ORDEN_PROG = [
        "PRIMERA_INFANCIA","INFANCIA","ADOLESCENCIA","JOVENES","ADULTEZ","VEJEZ",
        "RUTA_MATERNA_PRECONCEPCION","RUTA_MATERNA_IVE","RUTA_MATERNA_PRENATAL","RUTA_MATERNA_PARTO","RUTA_MATERNA_POSPARTO",
        "DI_PRIMERA_INFANCIA","DI_INFANCIA","DI_ADOLESCENCIA","DI_JOVENES","DI_ADULTEZ","DI_VEJEZ",
        "DI0007","DI0008","DI0009","DI00011","DI_SALUD_MENTAL",
        "RCV_RIESGO","RCV_DM","RCV_DM_HTA"
    ]
    entries = sorted(resultados.items(), key=lambda x: ORDEN_PROG.index(x[0]) if x[0] in ORDEN_PROG else 999)

    for pid, prog in entries:
        # Fila programa
        ws.merge_cells(f"A{row}:F{row}")
        prog_total = prog.get("total_fin" if usar_fin else "total", 0)
        ws[f"A{row}"] = f"▶ {prog.get('nombre', pid)} — Total: {prog_total}"
        ws[f"A{row}"].font = Font(bold=True, size=10, color="1E40AF")
        ws[f"A{row}"].fill = prog_fill
        ws[f"A{row}"].border = border
        row += 1

        for aid, act in (prog.get("actividades") or {}).items():
            sin_fin = (act.get("encontrados", 0)) - (act.get("encontrados_fin", 0))
            sumando = aid in sumar_sin_fin
            base = act.get("encontrados_fin", 0) if usar_fin else act.get("encontrados", 0)
            found = base + (sin_fin if usar_fin and sumando and sin_fin > 0 else 0)

            cells = [
                (1, f"    {act.get('descripcion', aid)}"),
                (2, act.get("archivo", "")),
                (3, found),
                (4, sin_fin if act.get("tiene_finalidad") and sin_fin > 0 else ""),
                (5, ""),
            ]
            for col, val in cells:
                c = ws.cell(row=row, column=col, value=val)
                c.border = border
                c.font = Font(size=10)
                if col == 3:
                    c.alignment = Alignment(horizontal="center")
                    c.font = Font(bold=True, size=10, color="166534" if found > 0 else "6B7280")
                if col == 4 and val:
                    c.alignment = Alignment(horizontal="center")
                    c.font = Font(size=10, color="92400E")
            row += 1

    # Pie
    row += 1
    ws[f"A{row}"] = f"Generado: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} · Evaluador Res. 3280 v0.5.0 · DUSAKAWI EPSI"
    ws[f"A{row}"].font = Font(italic=True, size=9, color="9CA3AF")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    nombre_archivo = f"preeval_{fecha}_{prestador_nombre[:20].replace(' ','_') if prestador_nombre else 'sin_prestador'}.xlsx"

    return send_file(buf, as_attachment=True, download_name=nombre_archivo,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route("/api/actas", methods=["GET"])
@login_required
def get_actas():
    actas = _load_actas()
    ips_id = request.args.get("ips_id")
    if ips_id:
        actas = [a for a in actas if a.get("ips_id") == ips_id]
    return jsonify({"actas": actas})

@app.route("/api/preeval/exportar-errores", methods=["POST"])
@login_required
def preeval_exportar_errores():
    """Genera Excel con una hoja por programa: pacientes con error + actividades faltantes."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    body = request.get_json() or {}
    hojas = body.get("hojas", [])
    prestador = body.get("prestador_nombre", "")
    fecha = body.get("fecha", datetime.datetime.now().strftime("%Y-%m-%d"))

    wb = Workbook()
    wb.remove(wb.active)

    thin = Side(style="thin", color="CCCCCC")
    bord = Border(left=thin, right=thin, top=thin, bottom=thin)

    HDR_PAC = ["N°","Tipo Doc","Núm. Doc","Edad","Sexo","Fecha Atención",
               "Código Dx","CUPS","Descripción actividad",
               "Finalidad registrada","Finalidad requerida","Tipo de error","Acción correctiva","Observación"]
    WID_PAC  = [5, 10, 16, 6, 6, 16, 12, 14, 40, 28, 28, 22, 38, 14]

    HDR_FALT = ["Descripción actividad","CUPS","Archivo","Meta","Conciliada","Discordancia","% Cumpl."]
    WID_FALT = [45, 14, 14, 10, 10, 12, 10]

    FILL_HDR_MAIN = PatternFill("solid", fgColor="1E3A5F")
    FILL_HDR_SEC  = PatternFill("solid", fgColor="7C3AED")
    FILL_ERR      = PatternFill("solid", fgColor="FEF2F2")
    FILL_DUP      = PatternFill("solid", fgColor="FFF7ED")
    FILL_FALT     = PatternFill("solid", fgColor="FEF9C3")
    FILL_FALT_HDR = PatternFill("solid", fgColor="B45309")

    def write_title(ws, text, ncols, row, fill):
        ws.merge_cells(f"A{row}:{get_column_letter(ncols)}{row}")
        c = ws.cell(row=row, column=1, value=text)
        c.font = Font(bold=True, size=11, color="FFFFFF")
        c.fill = fill
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[row].height = 18

    def write_headers(ws, headers, widths, row, fill):
        for col, (h, w) in enumerate(zip(headers, widths), 1):
            c = ws.cell(row=row, column=col, value=h)
            c.font = Font(bold=True, size=9, color="FFFFFF")
            c.fill = fill
            c.alignment = Alignment(horizontal="center", wrap_text=True)
            c.border = bord
            ws.column_dimensions[get_column_letter(col)].width = w
        ws.row_dimensions[row].height = 20

    for hoja in hojas:
        nombre = hoja.get("nombre", "Programa")[:31]
        rango  = hoja.get("rango", "")
        pac_err = hoja.get("pacientes_error", [])
        faltantes = hoja.get("faltantes", [])

        ws = wb.create_sheet(title=nombre)
        ncols = max(len(HDR_PAC), len(HDR_FALT))

        # Fila 1: título principal
        titulo = f"{nombre}{' — '+rango if rango else ''} | {prestador} | {fecha}"
        write_title(ws, titulo, ncols, 1, FILL_HDR_MAIN)

        # Fila 2: subtítulo norma
        ws.merge_cells(f"A2:{get_column_letter(ncols)}2")
        ws["A2"] = "Res. 3280 de 2018 / Res. 948 de 2026 — DUSAKAWI EPSI"
        ws["A2"].font = Font(italic=True, size=8, color="374151")
        ws["A2"].alignment = Alignment(horizontal="center")

        # ── SECCIÓN 1: PACIENTES CON ERROR ──────────────────────────────────
        write_title(ws, f"SECCIÓN 1 — Pacientes con error de finalidad ({len(pac_err)} registros)", len(HDR_PAC), 3, FILL_HDR_MAIN)
        write_headers(ws, HDR_PAC, WID_PAC, 4, FILL_HDR_MAIN)
        ws.freeze_panes = "A5"

        row = 5
        for n, pac in enumerate(pac_err, 1):
            dup = pac.get("duplicado", False)
            fill = FILL_DUP if dup else (FILL_ERR if pac.get("tipoError") else PatternFill())
            obs = "⚠ Duplicado" if dup else ""
            vals = [
                n,
                pac.get("tipoDoc",""),
                pac.get("numDoc",""),
                pac.get("edad",""),
                pac.get("sexo",""),
                pac.get("fechaAtencion",""),
                pac.get("dx",""),
                pac.get("cups",""),
                pac.get("descripcionCups",""),
                pac.get("finalidadRegistrada",""),
                pac.get("finReq",""),
                pac.get("tipoError",""),
                pac.get("aCorregir",""),
                obs,
            ]
            for col, v in enumerate(vals, 1):
                c = ws.cell(row=row, column=col, value=v)
                c.font = Font(size=9)
                c.border = bord
                c.alignment = Alignment(wrap_text=(col in (9,13)), vertical="top")
                c.fill = fill
            ws.row_dimensions[row].height = 14
            row += 1

        if not pac_err:
            ws.merge_cells(f"A{row}:{get_column_letter(len(HDR_PAC))}{row}")
            ws.cell(row=row, column=1, value="✅ Sin errores de finalidad para este programa").font = Font(italic=True, color="166534")
            row += 1

        row += 1  # fila vacía separadora

        # ── SECCIÓN 2: ACTIVIDADES FALTANTES ───────────────────────────────
        write_title(ws, f"SECCIÓN 2 — Actividades bajo la meta ({len(faltantes)} actividades)", len(HDR_FALT), row, FILL_FALT_HDR)
        row += 1
        write_headers(ws, HDR_FALT, WID_FALT, row, FILL_FALT_HDR)
        row += 1

        for falt in faltantes:
            pct = falt.get("pctCumpl", 0)
            fill_f = PatternFill("solid", fgColor="FEE2E2") if pct < 50 else FILL_FALT
            vals = [
                falt.get("descripcion",""),
                falt.get("cups",""),
                falt.get("archivo",""),
                falt.get("meta",0),
                falt.get("conciliada",0),
                falt.get("discordancia",0),
                f"{pct}%",
            ]
            for col, v in enumerate(vals, 1):
                c = ws.cell(row=row, column=col, value=v)
                c.font = Font(size=9)
                c.border = bord
                c.alignment = Alignment(wrap_text=(col==1), vertical="top")
                c.fill = fill_f
                if col in (4,5,6):
                    c.alignment = Alignment(horizontal="center")
            ws.row_dimensions[row].height = 14
            row += 1

        if not faltantes:
            ws.merge_cells(f"A{row}:{get_column_letter(len(HDR_FALT))}{row}")
            ws.cell(row=row, column=1, value="✅ Todas las actividades cumplen la meta").font = Font(italic=True, color="166534")

    if not wb.sheetnames:
        wb.create_sheet("Sin datos")
        wb.active["A1"] = "No se encontraron errores ni actividades faltantes."

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"Pacientes_Error_Finalidad_{fecha}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/api/actas", methods=["POST"])
@login_required
def create_acta():
    user = _get_current_user()
    if user.rol not in ["admin", "evaluador"]:
        return jsonify({"error": "Sin permisos"}), 403
    body = request.get_json() or {}
    sd = _session_data()
    resultados = sd.get("resultados") or {}
    actas = _load_actas()
    new_acta = {
        "id": str(uuid.uuid4()),
        "ips_id": body.get("ips_id", ""),
        "acta_num": body.get("acta_num", ""),
        "fecha_eval": body.get("fecha_eval", ""),
        "periodo_evaluado": body.get("periodo_evaluado", ""),
        "vigencia_contrato": body.get("vigencia_contrato", ""),
        "empresa": body.get("empresa", ""),
        "nit": body.get("nit", ""),
        "regimen": body.get("regimen", "SUBSIDIADO"),
        "municipio": body.get("municipio", ""),
        "lugar": body.get("lugar", "VALLEDUPAR"),
        "num_contrato": body.get("num_contrato", ""),
        "coordinador": body.get("coordinador", ""),
        "funcionarios": body.get("funcionarios", []),
        "puntos_a_tratar": body.get("puntos_a_tratar", ""),
        "objetivo": body.get("objetivo", ""),
        "desarrollo_conclusiones": body.get("desarrollo_conclusiones", ""),
        "parrafo_despues_grafico": body.get("parrafo_despues_grafico", ""),
        "observaciones": body.get("observaciones", ""),
        "resultados": resultados,
        "creado_por": user.username,
        "creado_en": datetime.datetime.now().isoformat(),
    }
    actas.append(new_acta)
    _save_actas(actas)
    return jsonify({"ok": True, "acta": new_acta})

@app.route("/api/actas/<acta_id>", methods=["GET"])
@login_required
def get_acta(acta_id):
    actas = _load_actas()
    a = next((a for a in actas if a["id"] == acta_id), None)
    if not a:
        return jsonify({"error": "No encontrada"}), 404
    return jsonify({"acta": a})

@app.route("/api/actas/<acta_id>", methods=["PUT"])
@login_required
def update_acta(acta_id):
    user = _get_current_user()
    if user.rol not in ["admin", "evaluador"]:
        return jsonify({"error": "Sin permisos"}), 403
    body = request.get_json() or {}
    actas = _load_actas()
    for a in actas:
        if a["id"] == acta_id:
            for k in ["acta_num","fecha_eval","periodo_evaluado","vigencia_contrato","empresa","nit",
                      "regimen","municipio","lugar","num_contrato","coordinador","funcionarios",
                      "puntos_a_tratar","objetivo","desarrollo_conclusiones","parrafo_despues_grafico","observaciones"]:
                if k in body: a[k] = body[k]
            a["modificado_en"] = datetime.datetime.now().isoformat()
            _save_actas(actas)
            return jsonify({"ok": True})
    return jsonify({"error": "No encontrada"}), 404

@app.route("/api/actas/<acta_id>", methods=["DELETE"])
@login_required
def delete_acta(acta_id):
    user = _get_current_user()
    if user.rol != "admin":
        return jsonify({"error": "Sin permisos"}), 403
    actas = _load_actas()
    actas = [a for a in actas if a["id"] != acta_id]
    _save_actas(actas)
    return jsonify({"ok": True})

@app.route("/api/generar-acta", methods=["POST"])
@login_required
def generar_acta():
    sd   = _session_data()
    body = request.get_json() or {}
    if not sd.get("resultados"):
        return jsonify({"error": "Primero ejecute la evaluación"}), 400
    info = sd.get("info_acta", {})
    info.update(body.get("info_acta", {}))
    programas_acta = []
    for prog_id, r in sd["resultados"].items():
        programas_acta.append({"id": prog_id, "nombre": r["nombre_acta"],
                                "exigido": r["total_exigido"], "reconocido": r["total_reconocido"],
                                "descuento": r["total_descuento"], "pct": r["pct_cumplimiento"]})
    total_e = sum(p["exigido"]    for p in programas_acta)
    total_r = sum(p["reconocido"] for p in programas_acta)
    total_d = sum(p["descuento"]  for p in programas_acta)
    datos_acta = {"programas": programas_acta, "total_exigido": total_e,
                  "total_reconocido": total_r, "total_descuento": total_d}
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE_DIR.parent))
        from evaluador_3280 import generar_acta_excel
    except ImportError:
        # Fallback: generador básico sin openpyxl extra (solo en Vercel sin el módulo padre)
        try:
            from acta_simple import generar_acta_excel
        except ImportError:
            return jsonify({"error": "Módulo generador no disponible en este entorno"}), 500
    out_path = _get_session_dir() / "ACTA_EVALUACION.xlsx"
    generar_acta_excel(datos_acta, info, str(out_path))
    # Incrementar contador de actas del IPS
    empresa = info.get("empresa","")
    if empresa:
        ips_list = _load_ips()
        for ips in ips_list:
            if ips["nombre"] == empresa:
                ips["num_actas"] = ips.get("num_actas",0) + 1
                break
        _save_ips(ips_list)
    return send_from_directory(str(out_path.parent), out_path.name, as_attachment=True,
                               download_name=f"ACTA_{info.get('empresa','IPS')}_{info.get('periodo','')}.xlsx")

@app.route("/api/estado")
@login_required
def estado():
    sd = _session_data()
    return jsonify({"archivos_cargados": list(sd["archivos"].keys()),
                    "tiene_metas": bool(sd.get("metas")),
                    "tiene_resultados": bool(sd.get("resultados")),
                    "registros": {k: len(v) for k,v in sd["archivos"].items()}})

def _load_config_mutable():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

def _save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

@app.route("/api/config/actividad", methods=["POST"])
@login_required
def update_actividad():
    user = _get_current_user()
    if user.rol not in ["admin", "evaluador"]:
        return jsonify({"error": "Sin permisos"}), 403
    body = request.get_json() or {}
    act_id = body.get("act_id")
    if not act_id:
        return jsonify({"error": "act_id requerido"}), 400
    prog_id = body.get("prog_id")  # required only for new activities
    cfg = _load_config_mutable()
    is_new = act_id not in cfg.get("actividades_base", {})
    if is_new:
        if not prog_id:
            return jsonify({"error": "prog_id requerido para nueva actividad"}), 400
        if prog_id not in [p["id"] for p in cfg.get("programas", [])]:
            return jsonify({"error": f"Programa '{prog_id}' no existe"}), 400
        cfg.setdefault("actividades_base", {})[act_id] = {
            "cups": [], "finalidad": [], "aplica_a": [], "archivo": "consultas", "descripcion": act_id
        }
        # Add act_id to the program's activity list
        for p in cfg.get("programas", []):
            if p["id"] == prog_id:
                p.setdefault("actividades", [])
                if act_id not in p["actividades"]:
                    p["actividades"].append(act_id)
    act = cfg["actividades_base"][act_id]
    if "cups" in body:        act["cups"]        = body["cups"]
    if "finalidad" in body:   act["finalidad"]   = body["finalidad"]
    if "aplica_a" in body:    act["aplica_a"]    = body["aplica_a"]
    if "archivo" in body:     act["archivo"]     = body["archivo"]
    if "descripcion" in body: act["descripcion"] = body["descripcion"]
    _save_config(cfg)
    return jsonify({"ok": True, "created": is_new})

@app.route("/api/config/cursos-vida", methods=["POST"])
@login_required
def update_cursos_vida():
    user = _get_current_user()
    if user.rol != "admin":
        return jsonify({"error": "Sin permisos"}), 403
    body = request.get_json() or {}
    cfg = _load_config_mutable()
    cfg["cursos_de_vida"] = body.get("cursos_de_vida", cfg["cursos_de_vida"])
    _save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/config/finalidades", methods=["POST"])
@login_required
def update_finalidades():
    user = _get_current_user()
    if user.rol != "admin":
        return jsonify({"error": "Sin permisos"}), 403
    body = request.get_json() or {}
    cfg = _load_config_mutable()
    cfg["finalidades"] = body.get("finalidades", cfg.get("finalidades", {}))
    _save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/config/rutas-diag", methods=["POST"])
@login_required
def update_rutas_diag():
    user = _get_current_user()
    if user.rol != "admin":
        return jsonify({"error": "Sin permisos"}), 403
    body = request.get_json() or {}
    cfg = _load_config_mutable()
    cfg["rutas_diag"] = body.get("rutas_diag", cfg.get("rutas_diag", {}))
    _save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/limpiar", methods=["POST"])
@login_required
def limpiar():
    sd = _session_data()
    sd.clear()
    sd.update({"archivos":{}, "metas":{}, "info_acta":{}, "resultados": None})
    return jsonify({"ok": True})

if __name__ == "__main__":
    print("\n🚀 Evaluador Res. 3280 v0.1 – DUSAKAWI EPSI")
    print("   Login: admin / admin123")
    print("   Abre tu navegador en: http://localhost:5050\n")
    app.run(debug=True, port=5050, host="0.0.0.0")
