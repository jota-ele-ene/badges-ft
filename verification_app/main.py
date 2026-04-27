from pathlib import Path
import json
import io
import csv
import uuid
import hashlib
import os
import html
import urllib.request
from io import BytesIO
from urllib.parse import quote_plus

from dotenv import load_dotenv
load_dotenv()

import jwt
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount
from openbadges_bakery import unbake
from generate_badges import (
    construir_assertion,
    guardar_assertion,
    hornear_png,
)
from PIL import Image, UnidentifiedImageError


MIN_BADGE_SIZE = 400
TARGET_BADGE_SIZE = (400, 400)

app = FastAPI(title="Open Badges Verifier")

BASE_URL_OVERRIDE = (os.getenv("BASE_URL") or "").strip().rstrip("/")
BASE_DIR = Path(__file__).resolve().parent.parent

ASSERTIONS_DIR = BASE_DIR / "output" / "assertions"
ASSERTIONS_DIR.mkdir(parents=True, exist_ok=True)

STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_DIR = BASE_DIR / "config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

BADGES_BAKED_DIR = BASE_DIR / "output" / "badges_baked"
BADGES_BAKED_DIR.mkdir(parents=True, exist_ok=True)

BADGE_ASSETS_DIR = BASE_DIR / "uploads" / "badge_assets"
BADGE_ASSETS_DIR.mkdir(parents=True, exist_ok=True)

JWT_SECRET = os.getenv("JWT_SECRET", "CAMBIA_ESTO_POR_UNA_CLAVE_MUY_LARGA_Y_SECRETA")
JWT_ALGORITHM = "HS256"
TOKEN_QUERY_PARAM = "token"

ACCEPTED_CODES = {
    code.strip().lower()
    for code in os.getenv("ACCEPTED_CODES", "").split(",")
    if code.strip()
}

PUBLIC_PATHS = {
    "/",
    "/login",
    "/verify-id",
    "/verify-png",
    "/verify-email-hash",
    "/favicon.ico",
}

PUBLIC_PREFIXES = (
    "/static/",
    "/badges-baked/",
    "/assertions/",
    "/uploaded-badges/",
    "/public-config/",
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/badges-baked", StaticFiles(directory=str(BADGES_BAKED_DIR)), name="badges-baked")
app.mount("/uploaded-badges", StaticFiles(directory=str(BADGE_ASSETS_DIR)), name="uploaded-badges")
app.mount("/public-config", StaticFiles(directory=str(CONFIG_DIR)), name="public-config")


def get_base_url(request: Request) -> str:
    if BASE_URL_OVERRIDE:
        return BASE_URL_OVERRIDE
    return str(request.base_url).rstrip("/")


def build_email_tkn(email: str) -> str:
    email_norm = (email or "").strip().lower()
    return hashlib.sha256(email_norm.encode("utf-8")).hexdigest().lower()


def extract_code_from_tkn(full_tkn: str) -> str:
    full_tkn = (full_tkn or "").strip().lower()
    if len(full_tkn) < 64:
        return ""
    positions = (1, 3, 7, 15, 31, 63)
    return "".join(full_tkn[i] for i in positions)


def create_jwt_for_email(email: str) -> str:
    full_tkn = build_email_tkn(email)
    payload = {"tkn": full_tkn}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


def get_request_token(request: Request | None) -> str:
    if request is None:
        return ""
    auth_header = (request.headers.get("Authorization") or "").strip()
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    query_token = (request.query_params.get(TOKEN_QUERY_PARAM) or "").strip()
    if query_token:
        return query_token
    return ""


def build_login_redirect(target_path: str = "") -> str:
    if target_path:
        return f"/login?next={quote_plus(target_path)}"
    return "/login"


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def append_token_to_path(path: str, token: str) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{TOKEN_QUERY_PARAM}={quote_plus(token)}"


def append_token_to_url(url: str, token: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{TOKEN_QUERY_PARAM}={quote_plus(token)}"


class JWTAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if is_public_path(path):
            return await call_next(request)

        token = get_request_token(request)
        if not token:
            return RedirectResponse(url=build_login_redirect(path), status_code=302)

        try:
            payload = decode_jwt_token(token)
            full_tkn = (payload.get("tkn") or "").strip().lower()
            short_code = extract_code_from_tkn(full_tkn)
            print(f"Access code: {short_code} | path={path}")
        except Exception:
            return RedirectResponse(url=build_login_redirect(path), status_code=302)

        if not full_tkn or not short_code or short_code not in ACCEPTED_CODES:
            return RedirectResponse(url=build_login_redirect(path), status_code=302)

        request.state.jwt_payload = payload
        request.state.jwt_token = token
        request.state.jwt_tkn = full_tkn
        request.state.jwt_code = short_code
        return await call_next(request)


app.add_middleware(JWTAuthMiddleware)


def listar_issuers():
    resultados = []
    for p in sorted(CONFIG_DIR.glob("*.issuer.json")):
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
            display_name = data.get("name") or p.name
        except Exception:
            display_name = p.name
        resultados.append((p.name, display_name))
    return resultados


def listar_badge_classes():
    resultados = []
    for p in sorted(CONFIG_DIR.glob("*.badge_class.json")):
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
            name = data.get("name")
            if isinstance(name, dict):
                display_name = name.get("ES") or name.get("EN") or next(iter(name.values()))
            else:
                display_name = name or p.name
        except Exception:
            display_name = p.name
        resultados.append((p.name, display_name))
    return resultados


def html_page(body: str, request: Request | None = None) -> HTMLResponse:
    header_path = STATIC_DIR / "header.html"
    footer_path = STATIC_DIR / "footer.html"

    try:
        header_html = header_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        header_html = ""

    try:
        footer_html = footer_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        footer_html = ""

    current_token = get_request_token(request) if request is not None else ""
    auth_bootstrap = f"""
    <script>
      window.APP_JWT_TOKEN = {json.dumps(current_token)};
      window.APP_TOKEN_PARAM = {json.dumps(TOKEN_QUERY_PARAM)};
    </script>
    <script src="/static/auth-guard.js"></script>
    """ if current_token else ""

    full_html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
      <meta charset="UTF-8">
      <title>Open Badges</title>
      <link rel="stylesheet" href="/static/styles.css">
      <style>
        .modal-backdrop {{ position: fixed; inset: 0; background: rgba(0,0,0,.45); display:none; align-items:center; justify-content:center; z-index:9999; }}
        .modal {{ background:#fff; padding:1rem 1.25rem; max-width:640px; width:92%; border-radius:10px; }}
        .row {{ margin-bottom: .85rem; }}
        label {{ font-weight: 600; display:block; margin-bottom:.25rem; }}
        input, select, button, textarea {{ font: inherit; }}
        .error {{ color:#b00020; }}
        .success {{ color:#176b2c; }}
        pre {{ white-space: pre-wrap; word-break: break-word; background:#f6f6f6; padding:1rem; border-radius:8px; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ccc; padding: 6px; text-align:left; vertical-align:top; }}
      </style>
    </head>
    <body>
      {header_html}
      <main>
        {body}
      </main>
      {auth_bootstrap}
      {footer_html}
    </body>
    </html>
    """
    return HTMLResponse(full_html)


def absolutize_url(base_url: str, value: str) -> str:
    value = (value or "").strip()
    if not value:
        return value
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("/"):
        return f"{base_url}{value}"
    return f"{base_url}/{value.lstrip('/')}"


def normalize_badge_class_urls(badge_class: dict, base_url: str, badge_filename: str | None = None) -> dict:
    if not isinstance(badge_class, dict):
        return badge_class

    data = dict(badge_class)

    if badge_filename:
        data["id"] = f"{base_url}/public-config/{badge_filename}"
    elif data.get("id"):
        data["id"] = absolutize_url(base_url, data["id"])

    if isinstance(data.get("issuer"), str) and data.get("issuer"):
        data["issuer"] = absolutize_url(base_url, data["issuer"])

    if data.get("image"):
        data["image"] = absolutize_url(base_url, data["image"])

    criteria = data.get("criteria")
    if isinstance(criteria, dict) and criteria.get("id"):
        criteria = dict(criteria)
        criteria["id"] = absolutize_url(base_url, criteria["id"])
        data["criteria"] = criteria

    return data


def normalize_issuer_urls(issuer: dict, base_url: str, issuer_filename: str | None = None) -> dict:
    if not isinstance(issuer, dict):
        return issuer

    data = dict(issuer)
    if issuer_filename:
        data["id"] = f"{base_url}/public-config/{issuer_filename}"
    elif data.get("id"):
        data["id"] = absolutize_url(base_url, data["id"])

    return data


def validar_y_normalizar_badge_png(image_bytes: bytes) -> bytes:
    if not image_bytes:
        raise ValueError("Debes subir un fichero PNG.")

    try:
        bio = BytesIO(image_bytes)
        img = Image.open(bio)
        img.load()
    except UnidentifiedImageError as e:
        raise ValueError(f"No se pudo identificar la imagen como PNG válido: {e}")
    except Exception as e:
        raise ValueError(f"Error al leer la imagen subida: {type(e).__name__}: {e}")

    if img.format != "PNG":
        raise ValueError(f"El fichero debe estar en formato PNG. Formato detectado: {img.format}")

    width, height = img.size
    if width != height:
        raise ValueError(f"La imagen debe ser cuadrada. Tamaño actual: {width}x{height}")

    if width < MIN_BADGE_SIZE or height < MIN_BADGE_SIZE:
        raise ValueError(f"La imagen debe medir al menos 400x400 píxeles. Tamaño actual: {width}x{height}")

    if img.mode not in ("RGBA", "RGB"):
        img = img.convert("RGBA")

    if width > MIN_BADGE_SIZE:
        img = img.resize(TARGET_BADGE_SIZE, Image.LANCZOS)

    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def validar_issuer_json(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("El JSON del issuer debe ser un objeto.")
    if not data.get("name"):
        raise ValueError("El issuer debe incluir el campo 'name'.")
    return data


def validar_badge_json(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("El JSON del badge_class debe ser un objeto.")
    if not data.get("name"):
        raise ValueError("El badge_class debe incluir el campo 'name'.")
    return data


def safe_name(name: str) -> str:
    return "".join(c for c in (name or "") if c.isalnum() or c in "-_").strip()


def build_email_verify_script(request: Request | None = None) -> str:
    token = get_request_token(request)
    verify_url = append_token_to_path("/verify-email-hash", token) if token else "/verify-email-hash"
    return f"""
    <script>
    function openEmailVerifyModal(assertionId) {{
      document.getElementById('assertion_id').value = assertionId;
      document.getElementById('email-modal').style.display = 'block';
    }}

    async function checkEmailHash(event) {{
      event.preventDefault();
      const assertionId = document.getElementById('assertion_id').value;
      const email = document.getElementById('email_to_check').value;

      const formData = new FormData();
      formData.append('assertion_id', assertionId);
      formData.append('email', email);

      const resp = await fetch({json.dumps(verify_url)}, {{
        method: 'POST',
        body: formData
      }});

      const data = await resp.json();
      const resultElem = document.getElementById('email-check-result');

      if (!data.ok) {{
        resultElem.textContent = 'Error: ' + (data.error || 'desconocido');
      }} else if (data.match) {{
        resultElem.textContent = 'El email coincide con el hash del badge.';
      }} else {{
        resultElem.textContent = 'El email NO coincide con el hash del badge.';
      }}
      return false;
    }}
    </script>
    """


def fetch_assertion_from_hosted_url(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "OpenBadgesVerifier/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = resp.read().decode("utf-8")
    return json.loads(payload)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    verify_path = STATIC_DIR / "verify.html"
    if not verify_path.is_file():
        return html_page("<h1>Error</h1><p>No se encuentra verify.html en /static.</p>", request)
    return html_page(verify_path.read_text(encoding="utf-8"), request)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = ""):
    body = f"""
    <h2>Acceso</h2>
    <form method="post" action="/login">
      <input type="hidden" name="next" value="{html.escape(next, quote=True)}">
      <label for="email">Correo electrónico</label><br>
      <input type="email" id="email" name="email" required style="min-width:320px"><br><br>
      <button type="submit">Entrar</button>
    </form>
    """
    return html_page(body, request)


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, email: str = Form(...), next: str = Form("")):
    email_norm = (email or "").strip().lower()
    if not email_norm:
        return html_page(
            '<h2>Acceso</h2><p class="error">Debes indicar un correo electrónico válido.</p><p><a href="/login">Volver</a></p>',
            request,
        )

    full_tkn = build_email_tkn(email_norm)
    short_code = extract_code_from_tkn(full_tkn)
    print(f"Access code: {short_code} | email={email_norm}")

    if short_code not in ACCEPTED_CODES:
        return html_page('<h2>Acceso denegado</h2><p><a href="/login">Volver</a></p>', request)

    token = create_jwt_for_email(email_norm)
    target = next.strip() or "/issuer"
    target_with_token = append_token_to_path(target, token)

    body = f"""
    <h2>Login correcto</h2>
    <p>Acceso concedido.</p>
    <script>window.location.replace({json.dumps(target_with_token)});</script>
    """
    return html_page(body, request)


@app.post("/verify-id", response_class=HTMLResponse)
async def verify_by_id(request: Request, assertion_id: str = Form(...)):
    ruta_json = ASSERTIONS_DIR / f"{assertion_id}.json"
    if not ruta_json.is_file():
        body = f"""
        <p style="color:red;">Assertion con ID <strong>{html.escape(assertion_id)}</strong> no encontrado.</p>
        <p><a href="/">Volver</a></p>
        """
        return html_page(body, request)

    with ruta_json.open("r", encoding="utf-8") as f:
        data = json.load(f)

    pretty = html.escape(json.dumps(data, ensure_ascii=False, indent=2))
    safe_assertion_id = html.escape(assertion_id, quote=True)

    body = f"""
    <h2>Resultado verificación por ID</h2>
    <p><strong>ID:</strong> {safe_assertion_id}</p>
    <pre>{pretty}</pre>
    <p><a href="/">Volver</a></p>
    <button onclick="openEmailVerifyModal('{safe_assertion_id}')">
      Verificar email del receptor
    </button>

    <div id="email-modal" style="display:none; margin-top:1rem;">
      <form onsubmit="return checkEmailHash(event);">
        <input type="hidden" id="assertion_id" value="{safe_assertion_id}" />
        <label>Email del receptor:</label>
        <input type="email" id="email_to_check" required />
        <button type="submit">Comprobar</button>
      </form>
      <div id="email-check-result" style="margin-top:.75rem;"></div>
    </div>
    """
    body += build_email_verify_script(request)
    return html_page(body, request)


@app.post("/verify-png", response_class=HTMLResponse)
async def verify_by_png(request: Request, file: UploadFile = File(...)):
    contents = await file.read()

    try:
        raw = unbake(io.BytesIO(contents))
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        raw = (raw or "").strip()

        try:
            assertion = json.loads(raw)
        except json.JSONDecodeError:
            if raw.startswith("http://") or raw.startswith("https://"):
                assertion = fetch_assertion_from_hosted_url(raw)
            else:
                body = f"""
                <h2>Resultado verificación por PNG</h2>
                <p><strong>Fichero:</strong> {html.escape(file.filename or '(sin nombre)')}</p>
                <h3>Contenido embebido (no JSON)</h3>
                <pre>{html.escape(raw)}</pre>
                <p><a href="/">Volver</a></p>
                """
                return html_page(body, request)

        pretty = html.escape(json.dumps(assertion, ensure_ascii=False, indent=2))
        assertion_url = assertion.get("id", "")
        assertion_id = assertion_url.rstrip("/").split("/")[-1] if assertion_url else ""
        safe_assertion_id = html.escape(assertion_id, quote=True)

        verify_button = ""
        verify_modal = ""
        if assertion_id:
            verify_button = f"""
            <button onclick="openEmailVerifyModal('{safe_assertion_id}')">
              Verificar email del receptor
            </button>
            """
            verify_modal = f"""
            <div id="email-modal" style="display:none; margin-top:1rem;">
              <form onsubmit="return checkEmailHash(event);">
                <input type="hidden" id="assertion_id" value="{safe_assertion_id}" />
                <label>Email del receptor:</label>
                <input type="email" id="email_to_check" required />
                <button type="submit">Comprobar</button>
              </form>
              <div id="email-check-result" style="margin-top:.75rem;"></div>
            </div>
            """

        body = f"""
        <h2>Resultado verificación por PNG</h2>
        <p><strong>Fichero:</strong> {html.escape(file.filename or '(sin nombre)')}</p>
        <h3>Assertion resuelto</h3>
        <pre>{pretty}</pre>
        <p><a href="/">Volver</a></p>
        {verify_button}
        {verify_modal}
        """

        if assertion_id:
            body += build_email_verify_script(request)

        return html_page(body, request)

    except Exception as e:
        body = f"""
        <h2>Error al verificar PNG</h2>
        <p><strong>Fichero:</strong> {html.escape(file.filename or '(sin nombre)')}</p>
        <p style="color:red;">No se pudo extraer assertion del PNG: {html.escape(str(e))}</p>
        <p><a href="/">Volver</a></p>
        """
        return html_page(body, request)


@app.get("/assertions/{assertion_id}", response_class=JSONResponse)
async def get_assertion(assertion_id: str):
    ruta_json = ASSERTIONS_DIR / f"{assertion_id}.json"
    if not ruta_json.is_file():
        return JSONResponse(
            status_code=404,
            content={"error": "Assertion no encontrado", "assertion_id": assertion_id},
        )

    with ruta_json.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return JSONResponse(content=data)


@app.get("/issuer", response_class=HTMLResponse)
async def issuer_form(request: Request):
    issuers = listar_issuers()
    badge_classes = listar_badge_classes()

    issuer_options = '<option value="">-- Selecciona issuer --</option><option value="__upload__">+ Subir otro issuer JSON...</option>'
    for filename, display_name in issuers:
        issuer_options += f'<option value="{html.escape(filename, quote=True)}">{html.escape(str(display_name))}</option>'

    badge_options = '<option value="">-- Selecciona badge class --</option><option value="__upload__">+ Subir otro badge JSON...</option>'
    for filename, display_name in badge_classes:
        badge_options += f'<option value="{html.escape(filename, quote=True)}">{html.escape(str(display_name))}</option>'

    token = get_request_token(request)
    preview_action = append_token_to_path("/issuer/preview", token) if token else "/issuer/preview"
    upload_issuer_action = append_token_to_path("/config/upload-issuer", token) if token else "/config/upload-issuer"
    upload_badge_action = append_token_to_path("/config/upload-badge", token) if token else "/config/upload-badge"

    issuer_select_options = "".join(
        [f'<option value="{html.escape(f, quote=True)}">{html.escape(str(d))}</option>' for f, d in issuers]
    )

    body = f"""
    <h2>Emitir badges</h2>
    <form method="post" action="{preview_action}" enctype="multipart/form-data" id="issuer-form">
      <p class="row"><label>Issuer existente</label><br><select name="issuer_choice" id="issuer_choice">{issuer_options}</select></p>
      <p class="row"><label>Badge class existente</label><br><select name="badge_choice" id="badge_choice">{badge_options}</select></p>
      <p class="row"><label>Email único opcional</label><br><input type="email" name="email"></p>
      <p class="row"><label>CSV opcional</label><br><input type="file" name="csv_file" accept=".csv,text/csv"></p>
      <p class="row"><label>Imagen PNG del badge opcional</label><br><input type="file" name="image_file" accept="image/png"></p>
      <button type="submit">Previsualizar emisión</button>
    </form>
    <p><a href="/">Volver a la verificación</a></p>

    <div class="modal-backdrop" id="issuer-modal-backdrop">
      <div class="modal">
        <h3>Subir nuevo issuer</h3>
        <form id="issuer-upload-form" enctype="multipart/form-data">
          <div class="row"><label>JSON issuer</label><input type="file" name="file" accept="application/json,.json" required></div>
          <div class="row"><button type="submit">Guardar issuer</button> <button type="button" onclick="closeModal('issuer')">Cancelar</button></div>
        </form>
        <div id="issuer-upload-result"></div>
      </div>
    </div>

    <div class="modal-backdrop" id="badge-modal-backdrop">
      <div class="modal">
        <h3>Subir nuevo badge</h3>
        <form id="badge-upload-form" enctype="multipart/form-data">
          <div class="row"><label>JSON badge_class</label><input type="file" name="file" accept="application/json,.json" required></div>
          <div class="row"><label>Imagen PNG del badge</label><input type="file" name="image_file" accept="image/png" required></div>
          <div class="row"><label>Issuer para enlazar</label><select name="issuer_filename" required>{issuer_select_options}</select></div>
          <div class="row"><button type="submit">Guardar badge</button> <button type="button" onclick="closeModal('badge')">Cancelar</button></div>
        </form>
        <div id="badge-upload-result"></div>
      </div>
    </div>

    <script>
      const issuerSelect = document.getElementById('issuer_choice');
      const badgeSelect = document.getElementById('badge_choice');

      function openModal(kind) {{ document.getElementById(kind + '-modal-backdrop').style.display = 'flex'; }}
      function closeModal(kind) {{ document.getElementById(kind + '-modal-backdrop').style.display = 'none'; }}

      issuerSelect.addEventListener('change', () => {{ if (issuerSelect.value === '__upload__') openModal('issuer'); }});
      badgeSelect.addEventListener('change', () => {{ if (badgeSelect.value === '__upload__') openModal('badge'); }});

      async function handleUpload(formId, actionUrl, resultId, selectId) {{
        const form = document.getElementById(formId);
        const result = document.getElementById(resultId);

        form.addEventListener('submit', async (e) => {{
          e.preventDefault();
          const fd = new FormData(form);
          const resp = await fetch(actionUrl, {{ method: 'POST', body: fd }});
          const data = await resp.json();

          if (!data.ok) {{
            result.innerHTML = '<p class="error">' + (data.error || 'Error') + '</p>';
            return;
          }}

          result.innerHTML = '<p class="success">Guardado correctamente.</p>';
          const select = document.getElementById(selectId);
          const opt = document.createElement('option');
          opt.value = data.filename;
          opt.textContent = data.display_name || data.filename;
          select.appendChild(opt);
          select.value = data.filename;
          closeModal(selectId === 'issuer_choice' ? 'issuer' : 'badge');
        }});
      }}

      handleUpload('issuer-upload-form', {json.dumps(upload_issuer_action)}, 'issuer-upload-result', 'issuer_choice');
      handleUpload('badge-upload-form', {json.dumps(upload_badge_action)}, 'badge-upload-result', 'badge_choice');
    </script>
    """
    return html_page(body, request)


@app.post("/issuer/preview", response_class=HTMLResponse)
async def issuer_preview(
    request: Request,
    email: str = Form(""),
    csv_file: UploadFile | None = File(None),
    image_file: UploadFile | None = File(None),
    issuer_choice: str = Form(""),
    badge_choice: str = Form(""),
):
    batch_id = str(uuid.uuid4())
    batch_dir = UPLOADS_DIR / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    image_path = None
    uploaded_image_name = "(sin subir PNG; se usará el del badge_class si existe)"

    if image_file and image_file.filename:
        image_path = batch_dir / "image.png"
        image_bytes = await image_file.read()
        try:
            image_bytes = validar_y_normalizar_badge_png(image_bytes)
        except ValueError as e:
            return html_page(
                f'<h2>Error en la imagen del badge</h2><p class="error">{html.escape(str(e))}</p><p><a href="/issuer">Volver al formulario</a></p>',
                request,
            )
        with image_path.open("wb") as f:
            f.write(image_bytes)
        uploaded_image_name = image_file.filename

    csv_path = None
    num_csv_rows = 0
    if csv_file and csv_file.filename:
        csv_path = batch_dir / "recipients.csv"
        csv_bytes = await csv_file.read()
        with csv_path.open("wb") as f:
            f.write(csv_bytes)
        with csv_path.open("r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        num_csv_rows = max(len(rows) - 1, 0) if rows else 0

    base_url = get_base_url(request)

    issuer = {}
    if issuer_choice:
        issuer_path = CONFIG_DIR / issuer_choice
        if issuer_path.is_file():
            with issuer_path.open("r", encoding="utf-8") as f:
                issuer = normalize_issuer_urls(json.load(f), base_url, issuer_choice)

    badge_class = {}
    if badge_choice:
        badge_path = CONFIG_DIR / badge_choice
        if badge_path.is_file():
            with badge_path.open("r", encoding="utf-8") as f:
                badge_class = normalize_badge_class_urls(json.load(f), base_url, badge_choice)

    config_data = {"issuer": issuer, "badge_class": badge_class}
    config_path = batch_dir / "config.json"
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(config_data, f, ensure_ascii=False, indent=2)

    criteria = badge_class.get("criteria", {})
    criteria_url = criteria.get("id") if isinstance(criteria, dict) else criteria

    email = (email or "").strip()
    if csv_path and num_csv_rows > 0:
        n_badges = num_csv_rows
        recipients_info = f"{num_csv_rows} receptores desde CSV."
    elif email:
        n_badges = 1
        recipients_info = f"1 receptor: {html.escape(email)}"
    else:
        n_badges = 0
        recipients_info = "Ningún receptor definido todavía (falta email o CSV)."

    token = get_request_token(request)

    preview_img_url = ""

    # Prioridad 1: imagen subida en este formulario
    if image_path and image_path.is_file():
        preview_img_url = f"{base_url}/uploaded-badges/{image_path.name}"

    # Prioridad 2: imagen del badge_class
    elif isinstance(badge_class, dict) and badge_class.get("image"):
        preview_img_url = badge_class.get("image")

    if preview_img_url and token:
        preview_img_url = append_token_to_url(preview_img_url, token)

    exec_action = append_token_to_path("/issuer/exec", token) if token else "/issuer/exec"

    if preview_img_url:
        preview_html = f'<p><strong>Miniatura:</strong><br><img src="{html.escape(preview_img_url, quote=True)}" alt="Miniatura badge" style="max-width:160px;max-height:160px;border:1px solid #ccc;padding:4px;background:#fff;"></p>'
    else:
        preview_html = '<p><strong>Miniatura:</strong> no disponible.</p>'

    body = f"""
    <h2>Previsualización del lote</h2>
    <p><strong>ID de lote:</strong> {html.escape(batch_id)}</p>
    <p><strong>Base URL servidor:</strong> {html.escape(base_url)}</p>
    <p>{recipients_info}</p>
    <p><strong>Fichero PNG subido:</strong> {html.escape(uploaded_image_name)}</p>
    <p><strong>Criteria URL:</strong> {html.escape(str(criteria_url or '(sin criteria)'))}</p>
    {preview_html}
    """

    if n_badges > 0:
        body += f"""
        <p>Se van a emitir <strong>{n_badges}</strong> badges si continúas.</p>
        <form method="post" action="{exec_action}">
          <input type="hidden" name="batch_id" value="{html.escape(batch_id, quote=True)}">
          <input type="hidden" name="use_csv" value="{'True' if csv_path and num_csv_rows > 0 else 'False'}">
          <input type="hidden" name="email" value="{html.escape(email, quote=True)}">
          <button type="submit">Emitir badges</button>
        </form>
        """
    else:
        body += '<p>No se emitirá ningún badge: debes indicar al menos un email o un CSV con receptores.</p>'

    body += '<p><a href="/issuer">Modificar datos de emisión</a></p>'
    return html_page(body, request)


@app.post("/issuer/exec", response_class=HTMLResponse)
@app.post("/issuer/exec", response_class=HTMLResponse)
async def issuer_exec(
    request: Request,
    batch_id: str = Form(...),
    use_csv: str = Form("False"),
    email: str = Form(""),
):
    batch_dir = UPLOADS_DIR / batch_id
    config_path = batch_dir / "config.json"
    image_path = batch_dir / "image.png"
    csv_path = batch_dir / "recipients.csv"

    if not config_path.is_file():
        return html_page(
            f'<p class="error">No se encontraron los ficheros necesarios para el lote <strong>{html.escape(batch_id)}</strong>.</p>'
            f'<p><a href="/issuer">Volver al formulario</a></p>',
            request,
        )

    with config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    badge_class = config.get("badge_class", {}) if isinstance(config, dict) else {}
    config.setdefault("image", {})

    # Prioridad 1: imagen subida específicamente para este lote
    if image_path.is_file():
        config["image"]["file"] = image_path.name
        config["image"]["directory"] = str(image_path.parent)

    # Prioridad 2: imagen del badge_class, resuelta a ruta local
    else:
        badge_image_url = ""
        if isinstance(badge_class, dict):
            badge_image_url = (badge_class.get("image") or "").strip()

        if not badge_image_url:
            return html_page(
                '<p class="error">El badge_class no tiene imagen configurada.</p>'
                '<p><a href="/issuer">Volver al formulario</a></p>',
                request,
            )

        badge_image_name = Path(badge_image_url.split("?", 1)[0]).name
        local_badge_image_path = BADGE_ASSETS_DIR / badge_image_name

        if not local_badge_image_path.is_file():
            return html_page(
                f'<p class="error">No se encontró la imagen local del badge_class: '
                f'<strong>{html.escape(str(local_badge_image_path))}</strong></p>'
                '<p><a href="/issuer">Volver al formulario</a></p>',
                request,
            )

        config["image"]["file"] = local_badge_image_path.name
        config["image"]["directory"] = str(local_badge_image_path.parent)

    base_url = get_base_url(request)
    hosted_base_url = f"{base_url}/assertions" + "/{assertion_id}"
    image_public_base_url = f"{base_url}/badges-baked"

    use_csv_flag = use_csv.lower() == "true"
    email = (email or "").strip()
    emitted = []

    output_dir = BASE_DIR / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    if use_csv_flag and csv_path.is_file():
        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                recipient_email = (row.get("email") or row.get("Email") or row.get("EMAIL") or "").strip()
                if not recipient_email:
                    continue

                nombre = row.get("nombre") or row.get("Nombre")
                apellido1 = row.get("apellido1") or row.get("Apellido1")
                apellido2 = row.get("apellido2") or row.get("Apellido2")
                assertion_id = str(uuid.uuid4())

                assertion = construir_assertion(
                    config=config,
                    assertion_id=assertion_id,
                    email=recipient_email,
                    nombre=nombre,
                    apellido1=apellido1,
                    apellido2=apellido2,
                    lang_override=None,
                    hosted_base_url=hosted_base_url,
                    image_public_base_url=image_public_base_url,
                    batch_id=batch_id,
                )
                ruta_json = guardar_assertion(assertion, output_dir, assertion_id)
                ruta_png = hornear_png(config, assertion, output_dir, assertion_id)
                emitted.append(
                    {
                        "email": recipient_email,
                        "assertion_id": assertion_id,
                        "json": str(ruta_json),
                        "png": str(ruta_png),
                    }
                )
    elif email:
        assertion_id = str(uuid.uuid4())
        assertion = construir_assertion(
            config=config,
            assertion_id=assertion_id,
            email=email,
            nombre=None,
            apellido1=None,
            apellido2=None,
            lang_override=None,
            hosted_base_url=hosted_base_url,
            image_public_base_url=image_public_base_url,
            batch_id=batch_id,
        )
        ruta_json = guardar_assertion(assertion, output_dir, assertion_id)
        ruta_png = hornear_png(config, assertion, output_dir, assertion_id)
        emitted.append(
            {
                "email": email,
                "assertion_id": assertion_id,
                "json": str(ruta_json),
                "png": str(ruta_png),
            }
        )
    else:
        return html_page(
            f'<p>Para el lote <strong>{html.escape(batch_id)}</strong> no se ha indicado ni CSV ni email válido.</p>'
            f'<p><a href="/issuer">Volver al formulario</a></p>',
            request,
        )

    rows_html = []
    token = get_request_token(request)
    for item in emitted:
        assertion_url = append_token_to_path(f"/assertions/{item['assertion_id']}", token) if token else f"/assertions/{item['assertion_id']}"
        png_name = Path(item["png"]).name
        png_url = append_token_to_path(f"/badges-baked/{png_name}", token) if token else f"/badges-baked/{png_name}"

        rows_html.append(
            f"<tr>"
            f"<td>{html.escape(item['email'])}</td>"
            f"<td>{html.escape(item['assertion_id'])}</td>"
            f"<td><a href='{html.escape(assertion_url, quote=True)}'>JSON</a></td>"
            f"<td><a href='{html.escape(png_url, quote=True)}'>PNG</a></td>"
            f"</tr>"
        )

    body = f"""
    <h2>Emisión completada</h2>
    <p>ID de lote: <strong>{html.escape(batch_id)}</strong></p>
    <p>Se han emitido <strong>{len(emitted)}</strong> badges.</p>
    <table>
      <thead><tr><th>Email</th><th>Assertion ID</th><th>Assertion URL</th><th>Imagen PNG</th></tr></thead>
      <tbody>{''.join(rows_html)}</tbody>
    </table>
    <p><a href="/issuer">Emitir otro lote</a></p>
    <p><a href="/">Volver a la verificación</a></p>
    """
    return html_page(body, request)

@app.post("/config/upload-issuer", response_class=JSONResponse)
async def upload_issuer(request: Request, file: UploadFile = File(...)):
    original_name = Path(file.filename or "issuer").stem
    filename_root = safe_name(original_name)
    if not filename_root:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Nombre inválido"})

    contents = await file.read()
    try:
        data = validar_issuer_json(json.loads(contents.decode("utf-8")))
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"JSON issuer inválido: {e}"})

    filename = f"{filename_root}.issuer.json"
    base_url = get_base_url(request)
    data = normalize_issuer_urls(data, base_url, filename)

    target_path = CONFIG_DIR / filename
    with target_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return JSONResponse(content={"ok": True, "filename": filename, "display_name": data.get("name", filename)})


@app.post("/config/upload-badge", response_class=JSONResponse)
async def upload_badge(
    request: Request,
    issuer_filename: str = Form(...),
    file: UploadFile = File(...),
    image_file: UploadFile = File(...),
):
    original_name = Path(file.filename or "badge").stem
    filename_root = safe_name(original_name)
    if not filename_root:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Nombre inválido"})

    issuer_path = CONFIG_DIR / issuer_filename
    if not issuer_path.is_file():
        return JSONResponse(status_code=400, content={"ok": False, "error": "Issuer no encontrado"})

    contents = await file.read()
    try:
        data = validar_badge_json(json.loads(contents.decode("utf-8")))
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"JSON badge_class inválido: {e}"})

    image_bytes = await image_file.read()
    try:
        image_bytes = validar_y_normalizar_badge_png(image_bytes)
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

    img_filename = f"{filename_root}.png"
    with (BADGE_ASSETS_DIR / img_filename).open("wb") as f:
        f.write(image_bytes)

    filename = f"{filename_root}.badge_class.json"
    base_url = get_base_url(request)

    data = normalize_badge_class_urls(data, base_url, filename)
    data["issuer"] = f"{base_url}/public-config/{issuer_filename}"
    data["image"] = f"{base_url}/uploaded-badges/{img_filename}"

    criteria = data.get("criteria")
    if isinstance(criteria, dict):
        criteria.setdefault("id", f"{base_url}/criteria/{filename_root}")

    target_path = CONFIG_DIR / filename
    with target_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    display_name = data.get("name", filename)
    if isinstance(display_name, dict):
        display_name = display_name.get("ES") or display_name.get("EN") or next(iter(display_name.values()))

    return JSONResponse(content={"ok": True, "filename": filename, "display_name": display_name})


@app.post("/verify-email-hash", response_class=JSONResponse)
async def verify_email_hash(request: Request, assertion_id: str = Form(...), email: str = Form(...)):
    ruta_json = ASSERTIONS_DIR / f"{assertion_id}.json"
    if not ruta_json.is_file():
        return JSONResponse(status_code=404, content={"ok": False, "error": "Assertion no encontrado"})

    with ruta_json.open("r", encoding="utf-8") as f:
        data = json.load(f)

    recipient = data.get("recipient", {})
    if not recipient.get("hashed"):
        return JSONResponse(content={"ok": False, "error": "El recipient no está hashed"})

    identity = recipient.get("identity", "")
    salt = recipient.get("salt", "")
    email_norm = email.strip().lower()
    to_hash = (salt + email_norm).encode("utf-8")
    digest = hashlib.sha256(to_hash).hexdigest()
    expected = f"sha256${digest}"
    match = identity == expected

    return JSONResponse(content={"ok": True, "match": match})


@app.get("/__routes", response_class=JSONResponse)
async def debug_routes():
    rutas = []
    for route in app.router.routes:
        if isinstance(route, Mount):
            rutas.append(f"MOUNT {route.path}")
        else:
            methods = sorted(getattr(route, "methods", []) or [])
            rutas.append(f"{','.join(methods)} {route.path}")
    return sorted(rutas)