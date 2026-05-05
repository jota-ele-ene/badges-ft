from pathlib import Path
import csv
import base64
import csv
import hashlib
import html
import io
import json
import logging
import os
import secrets
import httpx
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional
from urllib.parse import quote_plus

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from openbadges_bakery import unbake
from PIL import Image, UnidentifiedImageError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount

from generate_badges import (
    construir_assertion,
    guardar_assertion,
    hornear_png,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)
MIN_BADGE_SIZE = 400
TARGET_BADGE_SIZE = (400, 400)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Cache simple para requests externos (issuer/badge)
json_cache = {}

app = FastAPI(title="Open Badges Verifier")

BASE_URL_OVERRIDE = (os.getenv("BASE_URL") or "").strip().rstrip("/")
BASE_DIR = Path(__file__).resolve().parent.parent

ASSERTIONS_DIR = BASE_DIR / "output" / "assertions"
ASSERTIONS_DIR.mkdir(parents=True, exist_ok=True)

STATIC_DIR = BASE_DIR / "static"
IMAGES_DIR = BASE_DIR / "static" / "images"

UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_DIR = BASE_DIR / "config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

BADGES_BAKED_DIR = BASE_DIR / "output" / "badges_baked"
BADGES_BAKED_DIR.mkdir(parents=True, exist_ok=True)

BADGE_ASSETS_DIR = BASE_DIR / "uploads" / "badge_assets"
BADGE_ASSETS_DIR.mkdir(parents=True, exist_ok=True)

WALLED = os.getenv("WALLED", "false").strip().lower() == "true"
WALLED_USER = os.getenv("WALLED_USER", "").strip()
WALLED_PASS = os.getenv("WALLED_PASS", "").strip()
if WALLED and (not WALLED_USER or not WALLED_PASS):
    raise RuntimeError(
        "WALLED=true requires WALLED_USER and WALLED_PASS environment variables."
    )

ACCEPTED_CODES = {
    code.strip().lower()
    for code in os.getenv("ACCEPTED_CODES", "").split(",")
    if code.strip()
}

# Session management configuration
SESSION_TIMEOUT_MINUTES = 30
session_store = {}  # {session_id: {"code": str, "email": str, "created": datetime, "last_activity": datetime}}
SESSION_QUERY_PARAM = "session_id"

# Server start timestamp for Basic Auth realm (forces browser to re-prompt credentials)
SERVER_START_TIME = int(datetime.now().timestamp())

PUBLIC_PATHS = {
    "/",
    "/login",
    "/verify",
    "/verify-png",
    "/verify-id",
    "/verify-email-hash",
}

PUBLIC_PREFIXES = (
    "/static/",
    "/images/",
    "/badges-baked/",
    "/assertions/",
    "/uploaded-badges/",
    "/public-config/",
    "/verify-id/",
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/images", StaticFiles(directory=str(IMAGES_DIR)), name="images")
app.mount("/badges-baked", StaticFiles(directory=str(BADGES_BAKED_DIR)), name="badges-baked")
app.mount("/uploaded-badges", StaticFiles(directory=str(BADGE_ASSETS_DIR)), name="uploaded-badges")
app.mount("/public-config", StaticFiles(directory=str(CONFIG_DIR)), name="public-config")


@app.get("/favicon.ico")
async def favicon() -> FileResponse:
    favicon_path = STATIC_DIR / "favicon.ico"
    return FileResponse(favicon_path)


def get_base_url(request: Request) -> str:
    if BASE_URL_OVERRIDE:
        return BASE_URL_OVERRIDE
    return str(request.base_url).rstrip("/")





def create_session_for_code(code: str, email: str) -> str:
    """Create a new session for an access code. Returns session_id."""
    now = datetime.now()
    session_id = secrets.token_urlsafe(32)
    session_store[session_id] = {
        "code": code.lower().strip(),
        "email": email.lower().strip(),
        "created": now,
        "last_activity": now,
    }
    logger.info(f"Session created: {session_id}, code: {code}, email: {email}")
    return session_id


def validate_session(session_id: str, code: str) -> bool:
    """Validate if session exists and is not expired."""
    if not session_id or session_id not in session_store:
        return False
    
    session = session_store[session_id]
    now = datetime.now()
    created = session.get("created", now)
    
    # Check if session is expired (30 minutes)
    if (now - created).total_seconds() > SESSION_TIMEOUT_MINUTES * 60:
        del session_store[session_id]
        return False
    
    # Validate code matches
    session_code = (session.get("code") or "").lower().strip()
    if session_code != code.lower().strip():
        return False
    
    # Update last activity
    session["last_activity"] = now
    return True


def extract_code_from_email(email: str) -> str:
    """Extract short code from email hash."""
    email_norm = (email or "").strip().lower()
    full_tkn = hashlib.sha256(email_norm.encode("utf-8")).hexdigest().lower()
    positions = (1, 3, 7, 15, 31, 63)
    return "".join(full_tkn[i] for i in positions)


def parse_basic_auth_header(auth_header: str) -> tuple[str, str] | None:
    if not auth_header.lower().startswith("basic "):
        return None
    token = auth_header[6:].strip()
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username, password
    except Exception:
        return None


def basic_auth_is_valid(request: Request) -> bool:
    if not WALLED:
        return True
    auth_header = (request.headers.get("Authorization") or "").strip()
    creds = parse_basic_auth_header(auth_header)
    if not creds:
        return False
    username, password = creds
    return (
        secrets.compare_digest(username, WALLED_USER)
        and secrets.compare_digest(password, WALLED_PASS)
    )


def get_request_session_id(request: Request | None) -> str:
    if request is None:
        return ""
    query_session_id = (request.query_params.get(SESSION_QUERY_PARAM) or "").strip()
    if query_session_id:
        return query_session_id
    return ""


def get_request_code(request: Request | None) -> str:
    """Get access code from request (query param or headers)"""
    if request is None:
        return ""
    query_code = (request.query_params.get("code") or "").strip()
    if query_code:
        return query_code
    return ""


def build_login_redirect(target_path: str = "") -> str:
    if target_path:
        return f"/login?next={quote_plus(target_path)}"
    return "/login"


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def append_session_to_path(path: str, session_id: str, code: str) -> str:
    """Append session_id and code to path."""
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{SESSION_QUERY_PARAM}={quote_plus(session_id)}&code={quote_plus(code)}"


def append_session_to_url(url: str, session_id: str, code: str) -> str:
    """Append session_id and code to URL."""
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{SESSION_QUERY_PARAM}={quote_plus(session_id)}&code={quote_plus(code)}"


class SessionAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Always validate Basic Auth first if WALLED is true (even for public paths)
        if WALLED and not basic_auth_is_valid(request):
            return Response(
                status_code=401,
                content="Authentication required",
                headers={"WWW-Authenticate": f"Basic realm=\"Restricted-{SERVER_START_TIME}\""},
            )

        # If path is public, allow access without session
        if is_public_path(path):
            return await call_next(request)

        # For protected paths, require valid session with code
        session_id = get_request_session_id(request)
        code = get_request_code(request)
        
        if not session_id or not code:
            return RedirectResponse(
                url=build_login_redirect(path),
                status_code=302,
            )

        if not validate_session(session_id, code):
            return RedirectResponse(
                url=build_login_redirect(path),
                status_code=302,
            )

        # Store session info in request state for later use
        request.state.session_id = session_id
        request.state.session_code = code
        request.state.session_data = session_store.get(session_id, {})
        return await call_next(request)


app.add_middleware(SessionAuthMiddleware)


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
    head_path = STATIC_DIR / "head.html"
    header_path = STATIC_DIR / "header.html"
    footer_path = STATIC_DIR / "footer.html"

    try:
        head_html = head_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        head_html = ""

    try:
        header_html = header_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        header_html = ""

    try:
        footer_html = footer_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        footer_html = ""

    path = request.url.path if request is not None else "/"
    current_session_id = get_request_session_id(request) if request is not None else ""
    current_code = get_request_code(request) if request is not None else ""
    public_paths_js = json.dumps(list(PUBLIC_PATHS))
    public_prefixes_js = json.dumps(list(PUBLIC_PREFIXES))

    if is_public_path(path):
        auth_bootstrap = f"""
        <script>
          window.PUBLIC_PATHS = {public_paths_js};
          window.PUBLIC_PREFIXES = {public_prefixes_js};
        </script>
        """
    else:
        auth_bootstrap = f"""
        <script>
          window.APP_SESSION_ID = {json.dumps(current_session_id)};
          window.APP_SESSION_CODE = {json.dumps(current_code)};
          window.SESSION_QUERY_PARAM = {json.dumps(SESSION_QUERY_PARAM)};
          window.PUBLIC_PATHS = {public_paths_js};
          window.PUBLIC_PREFIXES = {public_prefixes_js};
        </script>
        <script src="/static/auth-guard.js"></script>
        """ if (current_session_id and current_code) else ""

    full_html = f"""
    <!DOCTYPE html>
    <html lang="es">
    {head_html}
    <body>
      {header_html}
      <main class="page-main">
        {body}
      </main>
      {auth_bootstrap}
      {footer_html}
    </body>
    </html>
    """
    return HTMLResponse(full_html)


def render_error_page(request: Request, title: str, message: str, return_path: str = "/") -> HTMLResponse:
    body = f"""
    <main class=\"page-main\">
      <div class=\"app-shell\">
        <section class=\"card\">
          <h2 class=\"card-title\">{html.escape(title)}</h2>
          <p class=\"alert alert-error\">{html.escape(message)}</p>
          <p style=\"margin-top:1rem;\">
            <a href=\"{html.escape(return_path, quote=True)}\" class=\"btn btn-secondary\">Volver</a>
          </p>
        </section>
      </div>
    </main>
    """
    return html_page(body, request)


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
    elif isinstance(data.get("id"), str) and data.get("id"):
        data["id"] = absolutize_url(base_url, data["id"])

    if isinstance(data.get("issuer"), str) and data.get("issuer"):
        data["issuer"] = absolutize_url(base_url, data["issuer"])

    if isinstance(data.get("image"), str) and data.get("image"):
        data["image"] = absolutize_url(base_url, data["image"])

    criteria = data.get("criteria")
    if isinstance(criteria, dict) and isinstance(criteria.get("id"), str) and criteria.get("id"):
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
    elif isinstance(data.get("id"), str) and data.get("id"):
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


def safe_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def build_email_verify_script(request: Request | None = None) -> str:
    session_id = get_request_session_id(request)
    code = get_request_code(request)
    verify_url = append_session_to_path("/verify-email-hash", session_id, code) if (session_id and code) else "/verify-email-hash"
    return f"""
    <script>
    function openEmailVerifyModal(assertionId) {{
      const assertionInput = document.getElementById('assertion_id');
      const modal = document.getElementById('email-modal-backdrop') || document.getElementById('email-modal');
      if (assertionInput) assertionInput.value = assertionId;
      if (modal) modal.style.display = 'block';
    }}

    function closeEmailModal() {{
      const modal = document.getElementById('email-modal-backdrop') || document.getElementById('email-modal');
      if (modal) modal.style.display = 'none';
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

      if (!resultElem) return false;

      if (!data.ok) {{
        resultElem.className = 'fail';
        resultElem.textContent = 'Error: ' + (data.error || 'desconocido');
      }} else if (data.match) {{
        resultElem.className = 'success';
        resultElem.textContent = '✓ El email coincide con el hash del badge.';
      }} else {{
        resultElem.className = 'error';
        resultElem.textContent = '✗ El email NO coincide con el hash del badge.';
      }}
      return false;
    }}
    </script>
    """


def render_assertion_result(
    request: Request,
    assertion: dict,
    source_filename: str | None = None,
) -> HTMLResponse:
    recipient = assertion.get("recipient") or {}
    recipient_hashed = isinstance(recipient, dict) and bool(recipient.get("hashed"))

    assertion_url = safe_text(assertion.get("id", "") or "")
    assertion_id = assertion_url.rstrip("/").split("/")[-1] if assertion_url else ""

    badge_url_raw = assertion.get("badge", "") or ""
    badge_url = badge_url_raw.get("id", "") if isinstance(badge_url_raw, dict) else safe_text(badge_url_raw)
    image_url_raw = assertion.get("image", "") or ""
    image_url = image_url_raw.get("url", "") if isinstance(image_url_raw, dict) else safe_text(image_url_raw)
    if image_url and not image_url.startswith(("http://", "https://", "/")):
        image_url = "/" + image_url.lstrip("/")
    issued_on = safe_text(assertion.get("issuedOn", "") or "")
    name = safe_text(assertion.get("name", "") or "")
    description = safe_text(assertion.get("description", "") or "")
    badge_name = safe_text(assertion.get("badgeName", "") or "")
    badge_description = safe_text(assertion.get("badgeDescription", "") or "")
    issuer_name = safe_text(assertion.get("issuerName", "") or "")

    issuer = assertion.get("issuer", {}) or {}
    if not isinstance(issuer, dict):
        issuer = {}
    verification = assertion.get("verification", {}) or {}
    if not isinstance(verification, dict):
        verification = {}
    verification_type = safe_text(verification.get("type", ""))
    verification_url = safe_text(verification.get("url", ""))
    batch = assertion.get("batch", {}) or {}
    if not isinstance(batch, dict):
        batch = {}
    batch_id = safe_text(batch.get("id", ""))

    issuer_description = safe_text(issuer.get("description", "")) if isinstance(issuer, dict) else ""
    issuer_website = safe_text(issuer.get("url", "")) if isinstance(issuer, dict) else ""
    issuer_id = safe_text(issuer.get("id", "")) if isinstance(issuer, dict) else ""

    pretty = html.escape(json.dumps(assertion, ensure_ascii=False, indent=2))

    assertion_id_safe = html.escape(assertion_id or "—")
    filename_safe = html.escape(source_filename or "(sin nombre)")
    badge_name_safe = html.escape(badge_name or "(sin nombre)")
    badge_desc_safe = html.escape(description or badge_description or "—")
    recipient_name_safe = html.escape(name or "(no especificado)")
    issuer_name_safe = html.escape(issuer_name or "(no especificado)")
    issued_on_safe = html.escape(issued_on or "(sin fecha)")
    batch_id_safe = html.escape(batch_id or "(sin batch)")
    assertion_url_safe = html.escape(assertion_url or "#", quote=True)
    badge_url_safe = html.escape(badge_url or "#", quote=True)
    image_url_safe = html.escape(image_url or "", quote=True)
    expiration_text_safe = "Sin caducidad (no expira)"
    issuer_description_safe = html.escape(issuer_description or "—")

    issuer_website_link = (
        f'<a href="{html.escape(issuer_website, quote=True)}" target="_blank" rel="noopener">Website</a>'
        if issuer_website else "—"
    )

    issuer_details_link = (
        f'<a href="{html.escape(issuer_id, quote=True)}" target="_blank" rel="noopener">Ver ficha completa del issuer</a>'
        if issuer_id else "—"
    )

    verification_status_text = "Válido (sin errores detectados)"
    _verification_text_safe = html.escape(
        f"{verification_type or '(sin tipo)'} → {verification_url or '(sin URL)'}"
    )

    recipient_alert_html = ""
    if recipient_hashed:
        recipient_alert_html = (
            '<div class="alert alert-warning" '
            'style="font-size: .8rem;margin-bottom: .25rem;margin-top: .6rem;'
            'background: #eef2f5;padding: 1em;border-radius: 1em;">'
                '<span>Para preservar la confidencialidad del titular de la credencial '
                'no mostramos en clara sus datos. Introduce un correo electrónico para '
                'verificar que coincide con los datos de la credencial.</span>'
                '<form onsubmit="return checkEmailHash(event);" '
                'style="display: flex;padding: 0.4em;margin: 0 20%;">'
                    f'<input type="hidden" id="assertion_id" value="{assertion_id_safe}">'
                    '<div class="form-row">'
                        '<input type="email" id="email_to_check" class="form-input" required="">'
                    '</div>'
                    '<button type="submit" class="btn btn-primary" '
                    'style="height: 1em;margin: 0 1em;font-size: small;">Comprobar</button>'
                '</form>'
                '<div id="email-check-result" style="font-size:.85rem;margin-left:20%"></div>'
            '</div>'
        )

    if assertion_id:
        email_button_html = (
            f'<button type="button" class="btn btn-secondary btn-sm" '
            f'style="margin-left:.5rem;" '
            f'onclick="openEmailVerifyModal(\'{assertion_id_safe}\')">'
            f'Verificar email del receptor'
            f'</button>'
        )
        email_modal_html = f"""
        <div class="modal-backdrop" id="email-modal-backdrop">
          <div class="modal">
            <h3>Verificar email del receptor</h3>
            <p style="font-size:.85rem;color:var(--text-muted);margin-bottom:.75rem;">
              Introduce el correo electrónico del receptor para comprobar si coincide con el hash almacenado en la assertion.
            </p>
            <form onsubmit="return checkEmailHash(event);">
              <input type="hidden" id="assertion_id" value="{assertion_id_safe}">
              <div class="form-row">
                <label class="form-label" for="email_to_check">Email del receptor</label>
                <input type="email" id="email_to_check" class="form-input" required>
              </div>
              <div id="email-check-result" style="margin-top:.5rem;font-size:.85rem;"></div>
              <div class="form-row" style="display:flex;justify-content:flex-end;gap:.75rem;margin-top:1rem;">
                <button type="button" class="btn btn-secondary" onclick="closeEmailModal()">Cancelar</button>
                <button type="submit" class="btn btn-primary">Comprobar</button>
              </div>
            </form>
          </div>
        </div>
        """
    else:
        email_button_html = ""
        email_modal_html = ""

    ctx = {
        "assertion_id_safe": assertion_id_safe,
        "filename_safe": filename_safe,
        "badge_name_safe": badge_name_safe,
        "badge_desc_safe": badge_desc_safe,
        "recipient_name_safe": recipient_name_safe,
        "issuer_name_safe": issuer_name_safe,
        "issued_on_safe": issued_on_safe,
        "expiration_text_safe": expiration_text_safe,
        "issuer_description_safe": issuer_description_safe,
        "issuer_website_link": issuer_website_link,
        "issuer_details_link": issuer_details_link,
        "assertion_url_safe": assertion_url_safe,
        "badge_url_safe": badge_url_safe,
        "image_url_safe": image_url_safe,
        "verification_status_text": verification_status_text,
        "assertion_json_pretty": pretty,
        "email_button_html": email_button_html,
        "email_modal_html": email_modal_html,
        "recipient_hashed": "true" if recipient_hashed else "",
        "recipient_alert_html": recipient_alert_html,
    }

    template_path = STATIC_DIR / "verify-png.html"
    template_html = template_path.read_text(encoding="utf-8")
    body = template_html.format(**ctx)
    body += build_email_verify_script(request)
    return html_page(body, request)


def resolve_local_json_resource(url: str) -> dict | None:
    """Resolve JSON resources from local filesystem if URL is from this server."""
    # Check if URL is from this server
    base_url = BASE_URL_OVERRIDE or ""
    if base_url and url.startswith(base_url):
        # Remove base URL to get relative path
        relative_path = url[len(base_url):]
    elif url.startswith("/public-config/"):
        # Already relative path
        relative_path = url
    else:
        return None

    # Check if it's a public-config resource
    if not relative_path.startswith("/public-config/"):
        return None

    # Extract filename from URL
    filename = relative_path[len("/public-config/"):].strip("/")
    if not filename:
        return None

    # Build full path
    file_path = CONFIG_DIR / filename
    if not file_path.is_file():
        return None

    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception:
        return None


async def fetch_json_resource(url: str) -> dict:
    if url in json_cache:
        return json_cache[url]

    # Try to resolve locally first (avoids HTTP requests for local resources)
    local_data = resolve_local_json_resource(url)
    if local_data is not None:
        json_cache[url] = local_data
        return local_data

    # If not local, make HTTP request
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(url, headers={
                "Accept": "application/json",
                "User-Agent": "OpenBadgesVerifier/1.0",
            })
            resp.raise_for_status()
            data = resp.json()
            json_cache[url] = data
            return data
        except Exception:
            json_cache[url] = {}
            return {}

    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return {}


async def fetch_issuer_from_badge(assertion: dict) -> dict:
    if not isinstance(assertion, dict):
        return assertion

    badge = assertion.get("badge") or {}
    if isinstance(badge, str):
        badge_obj = await fetch_json_resource(badge)
    elif isinstance(badge, dict):
        badge_obj = badge
    else:
        return assertion

    if badge_obj:
        assertion["badge"] = badge_obj

    issuer_obj: dict = {}
    issuer_id = ""

    issuer_field = badge_obj.get("issuer") or {} if isinstance(badge_obj, dict) else {}
    logger.info(f"issuer_field: {issuer_field}")
    if isinstance(issuer_field, str):
        issuer_id = issuer_field
        issuer_obj = await fetch_json_resource(issuer_id)
        logger.info(f"issuer_obj from fetch_json_resource: {issuer_obj}")
    elif isinstance(issuer_field, dict):
        issuer_obj = issuer_field
        issuer_id = issuer_obj.get("id", "") or ""
        logger.info(f"issuer_obj from dict: {issuer_obj}")
    else:
        logger.info("issuer_field is neither str nor dict")

    if not isinstance(issuer_obj, dict):
        return assertion

    issuer_from_assertion = assertion.get("issuer") or {}
    if isinstance(issuer_from_assertion, dict):
        issuer_obj = issuer_obj or {}
        for key, value in issuer_from_assertion.items():
            issuer_obj.setdefault(key, value)
        if not issuer_id:
            issuer_id = issuer_from_assertion.get("id", "") or issuer_id
        logger.info(f"issuer_obj after merge: {issuer_obj}")

    if issuer_id and "id" not in issuer_obj:
        issuer_obj["id"] = issuer_id

    if issuer_obj:
        assertion["issuer"] = issuer_obj
        logger.info(f"Final issuer_obj: {issuer_obj}")
        issuer_name = issuer_obj.get("name", "") or ""
        issuer_description = issuer_obj.get("description", "") or ""
        issuer_url = issuer_obj.get("url", "") or issuer_obj.get("website", "") or ""
        issuer_id_final = issuer_obj.get("id", "") or issuer_id

        assertion.setdefault("issuerName", issuer_name)
        assertion.setdefault("issuerDescription", issuer_description)
        if issuer_url:
            issuer_obj.setdefault("url", issuer_url)
        if issuer_id_final:
            issuer_obj.setdefault("id", issuer_id_final)

    return assertion


async def fetch_assertion_from_hosted_url(url: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(url, headers={
                "Accept": "application/json",
                "User-Agent": "OpenBadgesVerifier/1.0",
            })
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"No se pudo descargar la assertion alojada: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            raise ValueError(f"Error de red al descargar la assertion alojada: {exc}")
        except Exception as exc:
            raise ValueError(f"Error inesperado al descargar la assertion alojada: {exc}")

    t = data.get("type")
    is_assertion = (
        (isinstance(t, str) and "Assertion" in t)
        or (isinstance(t, list) and any("Assertion" in x for x in t))
    )

    if not is_assertion:
        raise ValueError("El recurso alojado no parece ser una Assertion Open Badges.")

    return await fetch_issuer_from_badge(data)


async def load_assertion_by_id(assertion_id: str) -> dict:
    ruta_json = ASSERTIONS_DIR / f"{assertion_id}.json"
    if not ruta_json.is_file():
        raise FileNotFoundError(assertion_id)

    with ruta_json.open("r", encoding="utf-8") as f:
        assertion = json.load(f)

    return await fetch_issuer_from_badge(assertion)


async def extract_assertion_from_png_bytes(contents: bytes) -> dict:
    raw = unbake(io.BytesIO(contents))
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    raw = (raw or "").strip()

    try:
        assertion = json.loads(raw)
        return await fetch_issuer_from_badge(assertion)
    except json.JSONDecodeError:
        if raw.startswith("http://") or raw.startswith("https://"):
            return await fetch_assertion_from_hosted_url(raw)
        raise ValueError("El fichero no contiene ninguna assertion embebida.")


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

    short_code = extract_code_from_email(email_norm)
    logger.info(
        f"Login computed shortcode={short_code}, accepted={short_code in ACCEPTED_CODES}, email={email_norm}"
    )

    if short_code not in ACCEPTED_CODES:
        return html_page('<h2>Acceso denegado</h2><p><a href="/login">Volver</a></p>', request)

    # Create session instead of JWT
    session_id = create_session_for_code(short_code, email_norm)
    target = next.strip() or "/issuer"
    target_with_session = append_session_to_path(target, session_id, short_code)

    body = f"""
    <h2>Login correcto</h2>
    <p>Acceso concedido.</p>
    <script>window.location.replace({json.dumps(target_with_session)});</script>
    """
    return html_page(body, request)


@app.get("/verify-id/{assertion_id}", response_class=HTMLResponse)
async def verify_by_id_get(request: Request, assertion_id: str):
    try:
        assertion = await load_assertion_by_id(assertion_id)
        return render_assertion_result(request, assertion, source_filename=f"assertion {assertion_id}")
    except FileNotFoundError:
        body = f"""
        <main class="page-main">
          <div class="app-shell">
            <section class="card">
              <h2 class="card-title">Resultado verificación</h2>
              <p class="card-subtitle">
                Assertion con ID <strong>{html.escape(assertion_id)}</strong> no encontrada.
              </p>
              <p style="margin-top:1rem;">
                <a href="/" class="btn btn-secondary">Volver al verificador</a>
              </p>
            </section>
          </div>
        </main>
        """
        return html_page(body, request)
    except Exception as e:
        body = f"""
        <main class="page-main">
          <div class="app-shell">
            <section class="card">
              <h2 class="card-title">Error al verificar</h2>
              <p class="alert alert-error">{html.escape(str(e))}</p>
              <p style="margin-top:1rem;">
                <a href="/" class="btn btn-secondary">Volver al verificador</a>
              </p>
            </section>
          </div>
        </main>
        """
        return html_page(body, request)


@app.post("/verify-id", response_class=HTMLResponse)
async def verify_by_id(request: Request, assertion_id: str = Form(...)):
    try:
        assertion = await load_assertion_by_id(assertion_id)
        return render_assertion_result(request, assertion, source_filename=f"assertion {assertion_id}")
    except FileNotFoundError:
        body = f"""
        <main class="page-main">
          <div class="app-shell">
            <section class="card">
              <h2 class="card-title">Resultado verificación</h2>
              <p class="card-subtitle">
                Assertion con ID <strong>{html.escape(assertion_id)}</strong> no encontrada.
              </p>
              <p style="margin-top:1rem;">
                <a href="/" class="btn btn-secondary">Volver al verificador</a>
              </p>
            </section>
          </div>
        </main>
        """
        return html_page(body, request)
    except Exception as e:
        body = f"""
        <main class="page-main">
          <div class="app-shell">
            <section class="card">
              <h2 class="card-title">Error al verificar</h2>
              <p class="alert alert-error">{html.escape(str(e))}</p>
              <p style="margin-top:1rem;">
                <a href="/" class="btn btn-secondary">Volver al verificador</a>
              </p>
            </section>
          </div>
        </main>
        """
        return html_page(body, request)


@app.post("/verify-png", response_class=HTMLResponse)
async def verify_by_png(request: Request, file: UploadFile = File(...)):
    contents = await file.read()
    if len(contents) > 5 * 1024 * 1024:  # 5MB límite
        raise HTTPException(status_code=413, detail="Archivo demasiado grande. Máximo 5MB.")

    try:
        assertion = await extract_assertion_from_png_bytes(contents)
        return render_assertion_result(
            request,
            assertion,
            source_filename=file.filename or "(sin nombre)",
        )
    except Exception as e:
        body = f"""
        <main class="page-main">
          <div class="app-shell">
            <section class="card">
              <h2 class="card-title">Error al verificar PNG</h2>
              <p class="card-subtitle">
                Fichero: <strong>{html.escape(file.filename or '(sin nombre)')}</strong>
              </p>
              <p class="alert alert-error">
                No se pudo extraer assertion del PNG: {html.escape(str(e))}
              </p>
              <p style="margin-top:1rem;">
                <a href="/" class="btn btn-secondary">Volver al verificador</a>
              </p>
            </section>
          </div>
        </main>
        """
        return html_page(body, request)


@app.post("/verify", response_class=HTMLResponse)
async def verify(
    request: Request,
    file: UploadFile | None = File(None),
    assertion_id: Optional[str] = Form(None),
    assertion_url: Optional[str] = Form(None),
):
    try:
        if file and file.filename:
            contents = await file.read()
            assertion = await extract_assertion_from_png_bytes(contents)
            source_name = file.filename or "(sin nombre)"
        elif assertion_id:
            assertion = await load_assertion_by_id(assertion_id)
            source_name = f"assertion {assertion_id}"
        elif assertion_url:
            assertion = await fetch_assertion_from_hosted_url(assertion_url.strip())
            source_name = assertion_url.strip()
        else:
            body = """
            <main class="page-main">
              <div class="app-shell">
                <section class="card">
                  <h2 class="card-title">Error de verificación</h2>
                  <p>No se ha proporcionado ni PNG, ni assertion_id, ni URL de assertion.</p>
                  <p style="margin-top:1rem;">
                    <a href="/" class="btn btn-secondary">Volver al verificador</a>
                  </p>
                </section>
              </div>
            </main>
            """
            return html_page(body, request)

        return render_assertion_result(request, assertion, source_filename=source_name)

    except FileNotFoundError:
        body = f"""
        <main class="page-main">
          <div class="app-shell">
            <section class="card">
              <h2 class="card-title">Resultado verificación</h2>
              <p class="card-subtitle">
                Assertion con ID <strong>{html.escape(assertion_id or '')}</strong> no encontrada.
              </p>
              <p style="margin-top:1rem;">
                <a href="/" class="btn btn-secondary">Volver al verificador</a>
              </p>
            </section>
          </div>
        </main>
        """
        return html_page(body, request)
    except Exception as e:
        body = f"""
        <main class="page-main">
          <div class="app-shell">
            <section class="card">
              <h2 class="card-title">Error al verificar</h2>
              <p class="alert alert-error">{html.escape(str(e))}</p>
              <p style="margin-top:1rem;">
                <a href="/" class="btn btn-secondary">Volver al verificador</a>
              </p>
            </section>
          </div>
        </main>
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

    session_id = get_request_session_id(request)
    code = get_request_code(request)
    preview_action = append_session_to_path("/issuer/preview", session_id, code) if (session_id and code) else "/issuer/preview"
    upload_issuer_action = append_session_to_path("/config/upload-issuer", session_id, code) if (session_id and code) else "/config/upload-issuer"
    upload_badge_action = append_session_to_path("/config/upload-badge", session_id, code) if (session_id and code) else "/config/upload-badge"

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

    if not issuer_choice:
        return render_error_page(request, "Issuer no seleccionado", "Debes seleccionar un issuer válido antes de continuar.", "/issuer")
    issuer_path = CONFIG_DIR / issuer_choice
    if not issuer_path.is_file():
        return render_error_page(
            request,
            "Issuer no encontrado",
            f"No se encontró el issuer seleccionado en la carpeta de configuración: {html.escape(issuer_choice)}.",
            "/issuer",
        )

    if not badge_choice:
        return render_error_page(request, "Badge class no seleccionada", "Debes seleccionar un badge class válido antes de continuar.", "/issuer")
    badge_path = CONFIG_DIR / badge_choice
    if not badge_path.is_file():
        return render_error_page(
            request,
            "Badge class no encontrada",
            f"No se encontró el badge class seleccionado en la carpeta de configuración: {html.escape(badge_choice)}.",
            "/issuer",
        )

    issuer = {}
    with issuer_path.open("r", encoding="utf-8") as f:
        issuer = normalize_issuer_urls(json.load(f), base_url, issuer_choice)

    badge_class = {}
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

    session_id = get_request_session_id(request)
    code = get_request_code(request)
    preview_img_url = ""

    if image_path and image_path.is_file():
        preview_img_url = f"{base_url}/uploaded-badges/{image_path.name}"
    elif isinstance(badge_class, dict) and badge_class.get("image"):
        preview_img_url = badge_class.get("image")

    if preview_img_url and session_id and code:
        preview_img_url = append_session_to_url(preview_img_url, session_id, code)

    exec_action = append_session_to_path("/issuer/exec", session_id, code) if (session_id and code) else "/issuer/exec"

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
    if not isinstance(badge_class, dict) or not badge_class:
        return render_error_page(
            request,
            "Configuración inválida",
            "La configuración del lote no contiene un badge_class válido.",
            "/issuer",
        )
    config.setdefault("image", {})

    if image_path.is_file():
        config["image"]["file"] = image_path.name
        config["image"]["directory"] = str(image_path.parent)
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
    session_id = get_request_session_id(request)
    code = get_request_code(request)
    for item in emitted:
        assertion_url = append_session_to_path(f"/assertions/{item['assertion_id']}", session_id, code) if (session_id and code) else f"/assertions/{item['assertion_id']}"
        png_name = Path(item["png"]).name
        png_url = append_session_to_path(f"/badges-baked/{png_name}", session_id, code) if (session_id and code) else f"/badges-baked/{png_name}"

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
    digest = hashlib.sha256((salt + email_norm).encode("utf-8")).hexdigest()
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