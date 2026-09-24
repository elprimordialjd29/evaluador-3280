#!/usr/bin/env python3
"""
Evaluador Res. 3280 – DUSAKAWI EPSI  v0.2
Servidor Flask con autenticación, roles y gestión de prestadores
Persistencia: Supabase (con fallback a JSON local)
"""
import json, os, datetime, uuid, hashlib
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

# ── Supabase ────────────────────────────────────────────────────────────────
SUPA_URL = os.environ.get("SUPABASE_URL", "")
SUPA_KEY = os.environ.get("SUPABASE_KEY", "")

_sb = None
def _get_sb():
    global _sb
    if _sb is None:
        try:
            from supabase import create_client
            _sb = create_client(SUPA_URL, SUPA_KEY)
        except Exception:
            pass
    return _sb

# ── Persistencia JSON (fallback local) ──────────────────────────────────────
USERS_FILE  = DATA_PATH / "users.json"
IPS_FILE    = DATA_PATH / "ips.json"
ACTAS_FILE  = DATA_PATH / "actas.json"

def _hash(pwd): return hashlib.sha256(pwd.encode()).hexdigest()

def _load_users():
    sb = _get_sb()
    if sb:
        try:
            rows = sb.table("usuarios").select("*").execute().data
            if rows:
                return [{"id": r["id"], "nombre": r["nombre"], "username": r["username"],
                         "password": r["password_hash"], "rol": r["rol"], "activo": r["activo"]}
                        for r in rows]
        except Exception:
            pass
    # Fallback JSON
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
    sb = _get_sb()
    sb_ok = False
    if sb:
        try:
            for u in users:
                sb.table("usuarios").upsert({
                    "id": u["id"], "nombre": u["nombre"], "username": u["username"],
                    "password_hash": u["password"], "rol": u["rol"], "activo": u.get("activo", True)
                }, on_conflict="username").execute()
            sb_ok = True
        except Exception:
            pass
    if not sb_ok:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)

def _load_ips():
    sb = _get_sb()
    if sb:
        try:
            rows = sb.table("prestadores").select("*").order("creado_en").execute().data
            if rows is not None:
                return [{
                    "id": r["id"], "nombre": r["nombre"], "nit": r.get("nit",""),
                    "num_contrato": r.get("num_contrato",""), "regimen": r.get("regimen",""),
                    "departamento": r.get("departamento",""), "municipio": r.get("municipio",""),
                    "rep_legal": r.get("rep_legal",""), "num_actas": r.get("num_actas", 0),
                    "activo": r.get("activo", True), "creado_por": r.get("creado_por",""),
                    "vigencia_inicio": r.get("vigencia_inicio",""),
                    "vigencia_fin": r.get("vigencia_fin",""),
                    "tipo_contrato": r.get("tipo_contrato","ASISTENCIAL"),
                    "lma": r.get("lma", {}),
                    "metas": _cargar_metas_supabase(r["id"]),
                } for r in rows]
        except Exception:
            pass
    if IPS_FILE.exists():
        with open(IPS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []

def _save_ips(ips):
    sb = _get_sb()
    sb_ok = False
    if sb:
        try:
            for p in ips:
                sb.table("prestadores").upsert({
                    "id": p["id"], "nombre": p["nombre"], "nit": p.get("nit",""),
                    "num_contrato": p.get("num_contrato",""), "regimen": p.get("regimen",""),
                    "departamento": p.get("departamento",""), "municipio": p.get("municipio",""),
                    "rep_legal": p.get("rep_legal",""), "num_actas": p.get("num_actas", 0),
                    "activo": p.get("activo", True), "creado_por": p.get("creado_por",""),
                    "vigencia_inicio": p.get("vigencia_inicio",""),
                    "vigencia_fin": p.get("vigencia_fin",""),
                    "tipo_contrato": p.get("tipo_contrato","ASISTENCIAL"),
                    "lma": p.get("lma", {}), "metas": p.get("metas", {}),
                }, on_conflict="id").execute()
            sb_ok = True
        except Exception:
            pass
    if not sb_ok:
        with open(IPS_FILE, "w", encoding="utf-8") as f:
            json.dump(ips, f, ensure_ascii=False, indent=2)

def _load_actas():
    sb = _get_sb()
    if sb:
        try:
            rows = sb.table("evaluaciones").select("*").order("creado_en", desc=True).execute().data
            if rows is not None:
                return [{
                    "id": r["id"], "prestador_id": r.get("prestador_id"),
                    "periodo": r.get("periodo",""), "fecha": str(r.get("fecha","")),
                    "total_exigido": float(r.get("total_exigido",0)),
                    "total_reconocido": float(r.get("total_reconocido",0)),
                    "total_descuento": float(r.get("total_descuento",0)),
                    "pct": float(r.get("pct_cumplimiento",0)),
                    "detalle": r.get("detalle_json"), "creado_por": r.get("creado_por","")
                } for r in rows]
        except Exception:
            pass
    if ACTAS_FILE.exists():
        with open(ACTAS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []

def _save_actas(actas):
    sb = _get_sb()
    sb_ok = False
    if sb:
        try:
            for a in actas:
                sb.table("evaluaciones").upsert({
                    "id": a["id"], "prestador_id": a.get("prestador_id"),
                    "periodo": a.get("periodo",""), "fecha": a.get("fecha"),
                    "total_exigido": a.get("total_exigido",0),
                    "total_reconocido": a.get("total_reconocido",0),
                    "total_descuento": a.get("total_descuento",0),
                    "pct_cumplimiento": a.get("pct",0),
                    "detalle_json": a.get("detalle"), "creado_por": a.get("creado_por","")
                }, on_conflict="id").execute()
            sb_ok = True
        except Exception:
            pass
    if not sb_ok:
        with open(ACTAS_FILE, "w", encoding="utf-8") as f:
            json.dump(actas, f, ensure_ascii=False, indent=2)

def _guardar_metas_supabase(prestador_id: str, metas: dict):
    """Guarda metas en tabla metas de Supabase (upsert por prestador+programa+actividad)."""
    sb = _get_sb()
    if not sb: return
    try:
        # Borrar metas anteriores de este prestador y reinsertar
        sb.table("metas").delete().eq("prestador_id", prestador_id).execute()
        rows = []
        for prog_id, acts in metas.items():
            if not isinstance(acts, dict): continue
            for act_id, valor in acts.items():
                rows.append({
                    "id": str(uuid.uuid4()),
                    "prestador_id": prestador_id,
                    "programa_id": prog_id,
                    "actividad_id": act_id,
                    "meta_upc": float(valor) if valor else 0,
                    "activo": True
                })
        if rows:
            sb.table("metas").insert(rows).execute()
    except Exception:
        pass

def _cargar_metas_supabase(prestador_id: str) -> dict:
    """Carga metas desde Supabase para un prestador."""
    sb = _get_sb()
    if not sb: return {}
    try:
        rows = sb.table("metas").select("*").eq("prestador_id", prestador_id).eq("activo", True).execute().data
        metas: dict = {}
        for r in rows:
            prog = r["programa_id"]
            act  = r["actividad_id"]
            metas.setdefault(prog, {})[act] = r.get("meta_upc", 0)
        return metas
    except Exception:
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
    u = next((u for u in users if u["id"] == uid), None)
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
            session["user_id"] = user["id"]
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
    return render_template("index.html", current_user=user)

@app.route("/api/config")
@login_required
def api_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    return jsonify({"programas": cfg["programas"], "actividades_base": cfg["actividades_base"],
                    "cursos_de_vida": cfg["cursos_de_vida"], "finalidades": cfg.get("finalidades", {})})

# ══════════════════════════════════════════════════════════════════════════
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
            return jsonify({"ok": True})
    return jsonify({"error": "No encontrado"}), 404

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
    ws[f"A{row}"] = f"Generado: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} · Evaluador Res. 3280 v0.4.3 · DUSAKAWI EPSI"
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
    """Genera Excel con pacientes que tienen errores de finalidad, por programa."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    body = request.get_json() or {}
    programas = body.get("programas", [])
    prestador = body.get("prestador_nombre", "")
    fecha = body.get("fecha", datetime.datetime.now().strftime("%Y-%m-%d"))

    wb = Workbook()
    wb.remove(wb.active)  # quitar hoja vacía inicial

    thin = Side(style="thin", color="CCCCCC")
    bord = Border(left=thin, right=thin, top=thin, bottom=thin)
    CAT_COLORS = {"Cursos de Vida": "1E40AF", "Ruta Materna": "9D174D",
                  "Demanda Inducida": "065F46", "RCV": "92400E"}
    HEADERS = ["N°","Tipo Doc","Núm. Doc","Edad","Sexo","Fecha Atención",
               "Código Dx","Programa","CUPS","Descripción actividad",
               "Finalidad registrada","Finalidad requerida","Tipo de error","Acción correctiva"]
    COL_WIDTHS = [5, 10, 16, 6, 6, 16, 12, 22, 14, 45, 30, 30, 22, 40]

    # Agrupar por categoría
    cat_sheets = {}
    for prog in programas:
        cat = prog.get("categoria", "Otros")
        cat_sheets.setdefault(cat, []).append(prog)

    cat_order = ["Cursos de Vida", "Ruta Materna", "Demanda Inducida", "RCV", "Otros"]
    for cat in cat_order:
        if cat not in cat_sheets:
            continue
        ws = wb.create_sheet(title=cat[:31])
        fill_hdr = PatternFill("solid", fgColor=CAT_COLORS.get(cat, "374151"))
        fill_prog = PatternFill("solid", fgColor="DBEAFE")
        fill_err = PatternFill("solid", fgColor="FEF2F2")

        # Título
        ws.merge_cells(f"A1:{get_column_letter(len(HEADERS))}1")
        ws["A1"] = f"Pacientes con errores de finalidad — {cat} | {prestador} | {fecha}"
        ws["A1"].font = Font(bold=True, size=12, color="FFFFFF")
        ws["A1"].fill = fill_hdr
        ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 20

        # Subtítulo norma
        ws.merge_cells(f"A2:{get_column_letter(len(HEADERS))}2")
        ws["A2"] = "Resolución 3280 de 2018 / Resolución 948 de 2026 — DUSAKAWI EPSI"
        ws["A2"].font = Font(italic=True, size=9, color="374151")
        ws["A2"].alignment = Alignment(horizontal="center")

        # Encabezados
        for col, (h, w) in enumerate(zip(HEADERS, COL_WIDTHS), 1):
            c = ws.cell(row=3, column=col, value=h)
            c.font = Font(bold=True, size=9, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1E3A5F")
            c.alignment = Alignment(horizontal="center", wrap_text=True)
            c.border = bord
            ws.column_dimensions[get_column_letter(col)].width = w
        ws.row_dimensions[3].height = 22

        row = 4
        n = 0
        for prog in cat_sheets[cat]:
            pnom = prog.get("nombre", prog.get("id", ""))
            for act in prog.get("actividades", []):
                desc = act.get("descripcion", "")
                cups = act.get("cups", "")
                fin_req = act.get("finalidadRequerida", "")
                for pac in act.get("pacientes", []):
                    n += 1
                    vals = [
                        n,
                        pac.get("tipoDoc",""),
                        pac.get("numDoc",""),
                        pac.get("edad",""),
                        pac.get("sexo",""),
                        pac.get("fechaAtencion",""),
                        pac.get("dx",""),
                        pnom,
                        cups,
                        desc,
                        pac.get("finalidadRegistrada",""),
                        fin_req,
                        pac.get("tipoError",""),
                        pac.get("aCorregir",""),
                    ]
                    bg = fill_err if pac.get("tipoError","")!="" else PatternFill()
                    for col, v in enumerate(vals, 1):
                        c = ws.cell(row=row, column=col, value=v)
                        c.font = Font(size=9)
                        c.border = bord
                        c.alignment = Alignment(wrap_text=(col in (10,14)), vertical="top")
                        if col >= 11:
                            c.fill = bg
                    ws.row_dimensions[row].height = 14
                    row += 1

        # Congelar paneles
        ws.freeze_panes = "A4"

    if not wb.sheetnames:
        wb.create_sheet("Sin errores")
        wb.active["A1"] = "No se encontraron pacientes con errores de finalidad."

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
