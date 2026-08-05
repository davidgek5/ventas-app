import os
import sys
from datetime import date, datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from sqlalchemy import create_engine, text


def resource_path(relative):
    """Ruta a archivos empaquetados (templates/static) dentro del .exe o en desarrollo."""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative)


def data_path(filename):
    """Ruta a archivos de datos (la base de datos local) junto al .exe, no dentro de él."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, filename)


app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("static"),
)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-esta-clave-por-una-segura")

# Si existe DATABASE_URL (Supabase/Postgres, en Render) se usa esa base de datos.
# Si no existe, se usa SQLite local (para el .exe que corre sin internet).
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    IS_POSTGRES = True
else:
    engine = create_engine(f"sqlite:///{data_path('ventas.db')}")
    IS_POSTGRES = False

LOCALES = ["Bolivar", "Fray", "Tobías", "Ceibos Pizza", "Ceibos Alitas"]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")  # cambia esta contraseña


def init_db():
    if IS_POSTGRES:
        ddl = """
            CREATE TABLE IF NOT EXISTS ventas (
                id SERIAL PRIMARY KEY,
                local TEXT NOT NULL,
                fecha TEXT NOT NULL,
                monto REAL NOT NULL,
                creado_en TEXT NOT NULL,
                UNIQUE(local, fecha)
            )
        """
    else:
        ddl = """
            CREATE TABLE IF NOT EXISTS ventas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local TEXT NOT NULL,
                fecha TEXT NOT NULL,
                monto REAL NOT NULL,
                creado_en TEXT NOT NULL,
                UNIQUE(local, fecha)
            )
        """
    with engine.begin() as conn:
        conn.execute(text(ddl))


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/")
def index():
    return render_template("index.html", locales=LOCALES)


@app.route("/registrar/<local>", methods=["GET", "POST"])
def registrar(local):
    if local not in LOCALES:
        return "Local no encontrado", 404

    if request.method == "POST":
        fecha = request.form.get("fecha") or date.today().isoformat()
        monto_raw = request.form.get("monto", "").replace(",", ".")
        try:
            monto = float(monto_raw)
            if monto < 0:
                raise ValueError
        except ValueError:
            flash("Ingresa un monto válido.", "error")
            return redirect(url_for("registrar", local=local))

        upsert = """
            INSERT INTO ventas (local, fecha, monto, creado_en)
            VALUES (:local, :fecha, :monto, :creado_en)
            ON CONFLICT (local, fecha) DO UPDATE SET monto = excluded.monto, creado_en = excluded.creado_en
        """
        with engine.begin() as conn:
            conn.execute(
                text(upsert),
                {
                    "local": local,
                    "fecha": fecha,
                    "monto": monto,
                    "creado_en": datetime.now().isoformat(timespec="seconds"),
                },
            )
        flash(f"Venta del {fecha} guardada para {local}.", "success")
        return redirect(url_for("registrar", local=local))

    with engine.connect() as conn:
        ultimos = conn.execute(
            text("SELECT fecha, monto FROM ventas WHERE local = :local ORDER BY fecha DESC LIMIT 10"),
            {"local": local},
        ).mappings().all()
    return render_template(
        "registrar.html", local=local, hoy=date.today().isoformat(), ultimos=ultimos
    )


def _resumen_local(conn, local):
    hoy = date.today()
    mes_actual = f"{hoy.year:04d}-{hoy.month:02d}"
    if hoy.month == 1:
        mes_anterior = f"{hoy.year - 1:04d}-12"
    else:
        mes_anterior = f"{hoy.year:04d}-{hoy.month - 1:02d}"

    total_actual = conn.execute(
        text("SELECT COALESCE(SUM(monto), 0) AS t FROM ventas WHERE local = :local AND fecha LIKE :mes"),
        {"local": local, "mes": f"{mes_actual}%"},
    ).mappings().one()["t"]
    total_anterior = conn.execute(
        text("SELECT COALESCE(SUM(monto), 0) AS t FROM ventas WHERE local = :local AND fecha LIKE :mes"),
        {"local": local, "mes": f"{mes_anterior}%"},
    ).mappings().one()["t"]
    return mes_actual, mes_anterior, float(total_actual), float(total_anterior)


@app.route("/registrar/<local>/api/serie")
def api_serie_local(local):
    """Historial de ventas del propio local (mes actual y anterior), sin necesitar admin."""
    if local not in LOCALES:
        return jsonify({"error": "local no encontrado"}), 404

    with engine.connect() as conn:
        mes_actual, mes_anterior, total_actual, total_anterior = _resumen_local(conn, local)
        serie = conn.execute(
            text("SELECT fecha, monto FROM ventas WHERE local = :local ORDER BY fecha ASC LIMIT 90"),
            {"local": local},
        ).mappings().all()

    return jsonify({
        "mes_actual": mes_actual,
        "mes_anterior": mes_anterior,
        "total_actual": total_actual,
        "total_anterior": total_anterior,
        "serie": [{"fecha": r["fecha"], "monto": r["monto"]} for r in serie],
    })


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_panel"))
        flash("Contraseña incorrecta.", "error")
    return render_template("login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@login_required
def admin_panel():
    with engine.connect() as conn:
        registros = conn.execute(
            text("SELECT local, fecha, monto FROM ventas ORDER BY fecha DESC, local ASC LIMIT 200")
        ).mappings().all()
    return render_template("admin.html", locales=LOCALES, registros=registros)


@app.route("/admin/api/resumen")
@login_required
def api_resumen():
    """Totales por local del mes actual y el anterior, más serie diaria del mes actual."""
    data = {}
    with engine.connect() as conn:
        for local in LOCALES:
            mes_actual, mes_anterior, total_actual, total_anterior = _resumen_local(conn, local)
            serie = conn.execute(
                text("SELECT fecha, monto FROM ventas WHERE local = :local AND fecha LIKE :mes ORDER BY fecha ASC"),
                {"local": local, "mes": f"{mes_actual}%"},
            ).mappings().all()
            data[local] = {
                "mes_actual": mes_actual,
                "mes_anterior": mes_anterior,
                "total_actual": total_actual,
                "total_anterior": total_anterior,
                "serie": [{"fecha": r["fecha"], "monto": r["monto"]} for r in serie],
            }
    return jsonify(data)


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
