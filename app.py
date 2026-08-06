import os
import sys
from datetime import date, datetime, timedelta
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


def _rango_mes(hoy):
    mes_actual = f"{hoy.year:04d}-{hoy.month:02d}"
    if hoy.month == 1:
        mes_anterior = f"{hoy.year - 1:04d}-12"
    else:
        mes_anterior = f"{hoy.year:04d}-{hoy.month - 1:02d}"
    return mes_actual, mes_anterior


def _rango_semana(hoy):
    inicio_actual = hoy - timedelta(days=hoy.weekday())
    fin_actual = inicio_actual + timedelta(days=6)
    inicio_anterior = inicio_actual - timedelta(days=7)
    fin_anterior = inicio_actual - timedelta(days=1)
    return inicio_actual, fin_actual, inicio_anterior, fin_anterior


def _suma_rango(conn, local, desde, hasta):
    total = conn.execute(
        text(
            "SELECT COALESCE(SUM(monto), 0) AS t FROM ventas "
            "WHERE local = :local AND fecha >= :desde AND fecha <= :hasta"
        ),
        {"local": local, "desde": desde, "hasta": hasta},
    ).mappings().one()["t"]
    return float(total)


def _resumen_local(conn, local):
    hoy = date.today()
    mes_actual, mes_anterior = _rango_mes(hoy)

    total_actual = conn.execute(
        text("SELECT COALESCE(SUM(monto), 0) AS t FROM ventas WHERE local = :local AND fecha LIKE :mes"),
        {"local": local, "mes": f"{mes_actual}%"},
    ).mappings().one()["t"]
    total_anterior = conn.execute(
        text("SELECT COALESCE(SUM(monto), 0) AS t FROM ventas WHERE local = :local AND fecha LIKE :mes"),
        {"local": local, "mes": f"{mes_anterior}%"},
    ).mappings().one()["t"]

    inicio_actual, fin_actual, inicio_anterior, fin_anterior = _rango_semana(hoy)
    semana_actual_total = _suma_rango(conn, local, inicio_actual.isoformat(), fin_actual.isoformat())
    semana_anterior_total = _suma_rango(conn, local, inicio_anterior.isoformat(), fin_anterior.isoformat())

    return {
        "mes_actual": mes_actual,
        "mes_anterior": mes_anterior,
        "total_actual": float(total_actual),
        "total_anterior": float(total_anterior),
        "semana_actual_inicio": inicio_actual.isoformat(),
        "semana_anterior_inicio": inicio_anterior.isoformat(),
        "semana_actual_total": semana_actual_total,
        "semana_anterior_total": semana_anterior_total,
    }


@app.route("/registrar/<local>/api/serie")
def api_serie_local(local):
    """Historial de ventas del propio local (mes y semana, actual y anterior), sin necesitar admin."""
    if local not in LOCALES:
        return jsonify({"error": "local no encontrado"}), 404

    with engine.connect() as conn:
        resumen = _resumen_local(conn, local)
        serie = conn.execute(
            text("SELECT fecha, monto FROM ventas WHERE local = :local ORDER BY fecha ASC LIMIT 90"),
            {"local": local},
        ).mappings().all()

    resumen["serie"] = [{"fecha": r["fecha"], "monto": r["monto"]} for r in serie]
    return jsonify(resumen)


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
            text("SELECT id, local, fecha, monto FROM ventas ORDER BY fecha DESC, local ASC LIMIT 200")
        ).mappings().all()
    return render_template("admin.html", locales=LOCALES, registros=registros)


@app.route("/admin/venta/<int:venta_id>/editar", methods=["GET", "POST"])
@login_required
def admin_editar_venta(venta_id):
    with engine.connect() as conn:
        venta = conn.execute(
            text("SELECT id, local, fecha, monto FROM ventas WHERE id = :id"),
            {"id": venta_id},
        ).mappings().first()

    if venta is None:
        flash("Ese registro ya no existe.", "error")
        return redirect(url_for("admin_panel"))

    if request.method == "POST":
        monto_raw = request.form.get("monto", "").replace(",", ".")
        fecha = request.form.get("fecha") or venta["fecha"]
        try:
            monto = float(monto_raw)
            if monto < 0:
                raise ValueError
        except ValueError:
            flash("Ingresa un monto válido.", "error")
            return redirect(url_for("admin_editar_venta", venta_id=venta_id))

        with engine.begin() as conn:
            conn.execute(
                text("UPDATE ventas SET fecha = :fecha, monto = :monto WHERE id = :id"),
                {"fecha": fecha, "monto": monto, "id": venta_id},
            )
        flash("Registro actualizado.", "success")
        return redirect(url_for("admin_panel"))

    return render_template("editar_venta.html", venta=venta)


@app.route("/admin/venta/<int:venta_id>/eliminar", methods=["POST"])
@login_required
def admin_eliminar_venta(venta_id):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ventas WHERE id = :id"), {"id": venta_id})
    flash("Registro eliminado.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/api/resumen")
@login_required
def api_resumen():
    """Totales por local: mes actual/anterior, semana actual/anterior, más serie diaria del mes actual."""
    data = {}
    with engine.connect() as conn:
        for local in LOCALES:
            resumen = _resumen_local(conn, local)
            serie = conn.execute(
                text("SELECT fecha, monto FROM ventas WHERE local = :local AND fecha LIKE :mes ORDER BY fecha ASC"),
                {"local": local, "mes": f"{resumen['mes_actual']}%"},
            ).mappings().all()
            resumen["serie"] = [{"fecha": r["fecha"], "monto": r["monto"]} for r in serie]
            data[local] = resumen
    return jsonify(data)


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
