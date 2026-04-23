from pathlib import Path
import json
import io
import csv
import uuid
import hashlib

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from openbadges_bakery import unbake  # para extraer assertion del PNG

from generate_badges import (
    cargar_configuracion,
    construir_assertion,
    guardar_assertion,
    hornear_png,
)

app = FastAPI(title="Open Badges Verifier")

BASE_DIR = Path(__file__).resolve().parent.parent
ASSERTIONS_DIR = BASE_DIR / "output" / "assertions"
STATIC_DIR = Path(__file__).resolve().parent / "static"
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR = BASE_DIR / "config"
BADGES_BAKED_DIR = BASE_DIR / "output" / "badges_baked"
BADGES_BAKED_DIR.mkdir(parents=True, exist_ok=True)

# Servir /static para otros recursos si los necesitas
app.mount(
    "/static", 
    StaticFiles(directory=str(STATIC_DIR)), 
    name="static"
    )

app.mount(
    "/badges-baked",
    StaticFiles(directory=str(BADGES_BAKED_DIR)),
    name="badges-baked",
)




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
            # name puede ser string o dict con idiomas
            if isinstance(name, dict):
                display_name = name.get("ES") or name.get("EN") or next(iter(name.values()))
            else:
                display_name = name or p.name
        except Exception:
            display_name = p.name
        resultados.append((p.name, display_name))
    return resultados

def html_page(body: str) -> HTMLResponse:
    html = f"""
    <!DOCTYPE html>
    <html lang="es">
      <head>
        <meta charset="UTF-8" />
        <title>Verificación de Open Badge</title>
      </head>
      <body>
        <h1>Verificación de Open Badge / Open Badge Verification</h1>
        {body}
      </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
async def home():
    # Devolver directamente el fichero verify.html estático
    return FileResponse(STATIC_DIR / "verify.html")

@app.get("/assertions/{assertion_id}", response_class=JSONResponse)
async def get_assertion(assertion_id: str):
    """
    Devuelve el JSON del assertion alojado (hosted).
    """
    ruta_json = ASSERTIONS_DIR / f"{assertion_id}.json"
    if not ruta_json.is_file():
        return JSONResponse(
            status_code=404,
            content={"error": "Assertion no encontrado", "assertion_id": assertion_id},
        )

    with ruta_json.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return JSONResponse(content=data)


@app.post("/verify-id", response_class=HTMLResponse)
async def verify_by_id(assertion_id: str = Form(...)):
    """
    Verificación simple por ID: lee el JSON local y lo presenta en HTML.
    """
    ruta_json = ASSERTIONS_DIR / f"{assertion_id}.json"
    if not ruta_json.is_file():
        body = f"""
        <p style="color:red;">Assertion con ID <strong>{assertion_id}</strong> no encontrado.</p>
        <p><a href="/">Volver</a></p>
        """
        return html_page(body)

    with ruta_json.open("r", encoding="utf-8") as f:
        data = json.load(f)

    pretty = json.dumps(data, ensure_ascii=False, indent=2)
    body = f"""
    <h2>Resultado verificación por ID</h2>
    <p><strong>ID:</strong> {assertion_id}</p>
    <pre>{pretty}</pre>
    <p><a href="/">Volver</a></p>
    <button onclick="openEmailVerifyModal('{assertion_id}')">
    Verificar email del receptor
    </button>

    <div id="email-modal" style="display:none;">
    <form onsubmit="return checkEmailHash(event);">
        <input type="hidden" id="assertion_id" value="{assertion_id}" />
        <label>Email del receptor:</label>
        <input type="email" id="email_to_check" required />
        <button type="submit">Comprobar</button>
    </form>
    <div id="email-check-result"></div>
    </div>
    """ + """
    <script>
    function openEmailVerifyModal(assertionId) {
    document.getElementById('assertion_id').value = assertionId;
    document.getElementById('email-modal').style.display = 'block';
    }

    async function checkEmailHash(event) {
    event.preventDefault();
    const assertionId = document.getElementById('assertion_id').value;
    const email = document.getElementById('email_to_check').value;

    const formData = new FormData();
    formData.append('assertion_id', assertionId);
    formData.append('email', email);

    const resp = await fetch('/verify-email-hash', {
        method: 'POST',
        body: formData
    });
    const data = await resp.json();
    const resultElem = document.getElementById('email-check-result');
    if (!data.ok) {
        resultElem.textContent = 'Error: ' + (data.error || 'desconocido');
    } else if (data.match) {
        resultElem.textContent = 'El email coincide con el hash del badge.';
    } else {
        resultElem.textContent = 'El email NO coincide con el hash del badge.';
    }
    return false;
    }
    </script>
    """
    return html_page(body)


@app.post("/verify-png", response_class=HTMLResponse)
async def verify_by_png(file: UploadFile = File(...)):
    """
    Verificación subiendo un PNG horneado:
    - Extrae el assertion usando unbake().
    - Muestra los datos básicos del assertion.
    """
    contents = await file.read()

    try:
        # unbake espera un file-like object; envolvemos los bytes en BytesIO
        raw = unbake(io.BytesIO(contents))  # puede ser JSON o URL
        try:
            assertion = json.loads(raw)
            pretty = json.dumps(assertion, ensure_ascii=False, indent=2)
            assertion_url = assertion.get("id", "")
            assertion_id = assertion_url.rstrip("/").split("/")[-1]
            body = f"""
            <h2>Resultado verificación por PNG</h2>
            <p><strong>Fichero:</strong> {file.filename}</p>
            <h3>Assertion embebido (JSON)</h3>
            <pre>{pretty}</pre>
            <p><a href="/">Volver</a></p>
            <button onclick="openEmailVerifyModal('{assertion_id}')">
            Verificar email del receptor
            </button>

            <div id="email-modal" style="display:none;">
            <form onsubmit="return checkEmailHash(event);">
                <input type="hidden" id="assertion_id" value="{assertion_id}" />
                <label>Email del receptor:</label>
                <input type="email" id="email_to_check" required />
                <button type="submit">Comprobar</button>
            </form>
            <div id="email-check-result"></div>
            </div>
            """ + """
            <script>
            function openEmailVerifyModal(assertionId) {
            document.getElementById('assertion_id').value = assertionId;
            document.getElementById('email-modal').style.display = 'block';
            }

            async function checkEmailHash(event) {
            event.preventDefault();
            const assertionId = document.getElementById('assertion_id').value;
            const email = document.getElementById('email_to_check').value;

            const formData = new FormData();
            formData.append('assertion_id', assertionId);
            formData.append('email', email);

            const resp = await fetch('/verify-email-hash', {
                method: 'POST',
                body: formData
            });
            const data = await resp.json();
            const resultElem = document.getElementById('email-check-result');
            if (!data.ok) {
                resultElem.textContent = 'Error: ' + (data.error || 'desconocido');
            } else if (data.match) {
                resultElem.textContent = 'El email coincide con el hash del badge.';
            } else {
                resultElem.textContent = 'El email NO coincide con el hash del badge.';
            }
            return false;
            }
            </script>
            """
        except json.JSONDecodeError:
            # No es JSON, asumimos que es una URL u otro tipo de string
            body = f"""
            <h2>Resultado verificación por PNG</h2>
            <p><strong>Fichero:</strong> {file.filename}</p>
            <h3>Contenido embebido (no JSON)</h3>
            <pre>{raw}</pre>
            <p><a href="/">Volver</a></p>
            """
    except Exception as e:
        body = f"""
        <h2>Error al verificar PNG</h2>
        <p><strong>Fichero:</strong> {file.filename}</p>
        <p style="color:red;">No se pudo extraer assertion del PNG: {e}</p>
        <p><a href="/">Volver</a></p>
        """

    return html_page(body)

@app.get("/issuer", response_class=HTMLResponse)
async def issuer_form():
    issuers = listar_issuers()          # lista de (filename, display_name)
    badge_classes = listar_badge_classes()

    # Opciones de issuer
    issuer_options = '<option value="">-- Selecciona issuer --</option>'
    for filename, display_name in issuers:
        issuer_options += f'<option value="{filename}">{display_name}</option>'
    issuer_options += '<option value="__new__">[+] Cargar nuevo issuer...</option>'

    # Opciones de badge class
    badge_options = '<option value="">-- Selecciona badge --</option>'
    for filename, display_name in badge_classes:
        badge_options += f'<option value="{filename}">{display_name}</option>'
    badge_options += '<option value="__new__">[+] Cargar nuevo badge...</option>'

    body = f"""
    <h2>Emisión de badges / Badge Issuer</h2>
    <form id="issuer_form" action="/issuer/preview" method="post" enctype="multipart/form-data">
    <h3>Issuer</h3>
    <select name="issuer_choice" id="issuer_choice" required>
        {issuer_options}
    </select>

    <h3>Badge Class</h3>
    <select name="badge_choice" id="badge_choice" required>
        {badge_options}
    </select>

    <h3>2. Receptores / Recipients</h3>
    <p>Puedes indicar un solo email o subir un CSV.</p>
    <label for="email">Email único (opcional):</label>
    <input type="email" id="email" name="email" />

    <br /><br />
    <label for="csv_file">CSV de contactos (opcional):</label>
    <input type="file" id="csv_file" name="csv_file" accept=".csv" />

    <h3>3. Imagen PNG del badge / Badge PNG image</h3>
    <input type="file" id="image_file" name="image_file" accept="image/png" required />

    <br /><br />
    <button type="submit">Previsualizar emisión / Preview issuance</button>
    </form>

    <p><a href="/">Volver a la verificación</a></p>
    <div id="config-modal" style="display:none;">
    <form onsubmit="return uploadConfigJson(event);">
        <label>Tipo:</label>
        <select id="config_kind">
        <option value="issuer">Issuer</option>
        <option value="badge">BadgeClass</option>
        </select>
        <label>Nombre base (sin extensión):</label>
        <input type="text" id="config_name" required />
        <label>Fichero JSON:</label>
        <input type="file" id="config_file" accept="application/json" required />
        <button type="submit">Subir</button>
    </form>
    <div id="config-upload-result"></div>
    </div>
    """ + """
    <script>
    document.querySelector('select[name="issuer_choice"]').addEventListener('change', function() {
    if (this.value === '__new__') {
        openConfigModal('issuer');
    }
    });
    document.querySelector('select[name="badge_choice"]').addEventListener('change', function() {
    if (this.value === '__new__') {
        openConfigModal('badge');
    }
    });

    function openConfigModal(kind) {
    document.getElementById('config_kind').value = kind;
    document.getElementById('config-modal').style.display = 'block';
    }

    async function uploadConfigJson(event) {
    event.preventDefault();
    const kind = document.getElementById('config_kind').value;
    const name = document.getElementById('config_name').value;
    const fileInput = document.getElementById('config_file');
    if (!fileInput.files.length) return false;

    const formData = new FormData();
    formData.append('kind', kind);
    formData.append('name', name);
    formData.append('file', fileInput.files[0]);

    const resp = await fetch('/config/upload-json', {
        method: 'POST',
        body: formData
    });
    const data = await resp.json();
    const resultElem = document.getElementById('config-upload-result');
    if (!data.ok) {
        resultElem.textContent = 'Error: ' + (data.error || 'desconocido');
    } else {
        resultElem.textContent = 'Subido como ' + data.filename + '. Recarga la página para verlo en el selector.';
    }
    return false;
    }
    </script>
    """
    return html_page(body)

@app.post("/issuer/preview", response_class=HTMLResponse)
async def issuer_preview(
    request: Request,
    config_file: UploadFile | None = File(None),
    email: str = Form(""),
    csv_file: UploadFile | None = File(None),
    image_file: UploadFile = File(...),
    issuer_choice: str = Form(""),
    badge_choice: str = Form(""),
):
    """
    Recibe:
    - Fichero JSON de emisión.
    - Email opcional.
    - CSV opcional.
    - PNG de imagen.
    Guarda todo en un lote dentro de uploads/ y muestra previsualización.
    """
    # Crear ID de lote
    batch_id = str(uuid.uuid4())
    batch_dir = UPLOADS_DIR / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    # Guardar config JSON solo si se ha subido alguno
    base_config = {}
    config_path = batch_dir / "config.json"

    if config_file and config_file.filename:
        config_bytes = await config_file.read()
        try:
            base_config = json.loads(config_bytes.decode("utf-8"))
        except Exception:
            base_config = {}
    else:
        base_config = {}

    # Guardar imagen
    image_path = batch_dir / "image.png"
    image_bytes = await image_file.read()
    with image_path.open("wb") as f:
        f.write(image_bytes)

    # Guardar CSV si existe y contar filas
    csv_path = None
    num_csv_rows = 0
    if csv_file and csv_file.filename:
        csv_path = batch_dir / "recipients.csv"
        csv_bytes = await csv_file.read()
        with csv_path.open("wb") as f:
            f.write(csv_bytes)
        # Contar filas (sin cabecera)
        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)
            if rows:
                num_csv_rows = len(rows) - 1  # restamos cabecera
            else:
                num_csv_rows = 0

    # base_config viene de config_file si se subió, o {} si no
    base_config = {}
    if config_file and config_file.filename:
        try:
            config_bytes = await config_file.read()
            base_config = json.loads(config_bytes.decode("utf-8"))
        except Exception:
            base_config = {}
    else:
        base_config = {}

    # 1) Resolver issuer
    issuer = {}
    if issuer_choice and issuer_choice != "__new__":
        issuer_path = CONFIG_DIR / issuer_choice
        try:
            with issuer_path.open("r", encoding="utf-8") as f:
                issuer = json.load(f)
        except Exception:
            issuer = {}
    else:
        issuer = base_config.get("issuer", {})

    # 2) Resolver badge_class
    badge_class = {}
    if badge_choice and badge_choice != "__new__":
        badge_path = CONFIG_DIR / badge_choice
        try:
            with badge_path.open("r", encoding="utf-8") as f:
                badge_class = json.load(f)
        except Exception:
            badge_class = {}
    else:
        badge_class = base_config.get("badge_class", {})

    # 3) Construir config_data final
    config_data = base_config
    config_data["issuer"] = issuer
    config_data["badge_class"] = badge_class

    # 4) Guardar config.json del lote
    config_path = batch_dir / "config.json"
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(config_data, f, ensure_ascii=False, indent=2)

    issuer_name = issuer.get("name", "(sin nombre)")
    badge_name = badge_class.get("name", "(sin nombre)")
    criteria = badge_class.get("criteria", {})
    criteria_url = criteria.get("id") if isinstance(criteria, dict) else criteria

    # Calcular N badges a emitir
    email = (email or "").strip()
    if csv_path and num_csv_rows > 0:
        n_badges = num_csv_rows
        recipients_info = f"{num_csv_rows} receptores desde CSV."
    elif email:
        n_badges = 1
        recipients_info = f"1 receptor: {email}"
    else:
        n_badges = 0
        recipients_info = "Ningún receptor definido todavía (falta email o CSV)."

    # URL base del servidor (se usará luego para hosted_base_url)
    base_url = str(request.base_url)

    body = f"""
    <h2>Previsualización de emisión</h2>
    <p><strong>ID de lote:</strong> {batch_id}</p>
    <p><strong>Base URL servidor:</strong> {base_url}</p>

    <h3>Issuer</h3>
    <ul>
      <li><strong>Nombre:</strong> {issuer_name}</li>
      <li><strong>ID:</strong> {issuer.get("id", "")}</li>
      <li><strong>URL:</strong> {issuer.get("url", "")}</li>
      <li><strong>Email:</strong> {issuer.get("email", "")}</li>
    </ul>

    <h3>Badge</h3>
    <ul>
      <li><strong>Nombre:</strong> {badge_name}</li>
      <li><strong>ID:</strong> {badge_class.get("id", "")}</li>
      <li><strong>Criteria URL:</strong> {criteria_url}</li>
    </ul>

    <h3>Receptores</h3>
    <p>{recipients_info}</p>

    <h3>Imagen</h3>
    <p>Fichero subido: {image_file.filename}</p>
    """

    if n_badges > 0:
        body += f"""
        <hr />
        <p>Se van a emitir <strong>{n_badges}</strong> badges si continúas.</p>
        <form action="/issuer/exec" method="post">
          <input type="hidden" name="batch_id" value="{batch_id}" />
          <input type="hidden" name="use_csv" value="{bool(csv_path and num_csv_rows > 0)}" />
          <input type="hidden" name="email" value="{email}" />
          <button type="submit">Emitir {n_badges} badges</button>
        </form>
        """
    else:
        body += """
        <hr />
        <p style="color:red;">No se emitirá ningún badge: debes indicar al menos un email o un CSV con receptores.</p>
        <p><a href="/issuer">Volver al formulario</a></p>
        """

    body += '<p><a href="/issuer">Modificar datos de emisión</a></p>'
    return html_page(body)

@app.post("/issuer/exec", response_class=HTMLResponse)
async def issuer_exec(
    request: Request,
    batch_id: str = Form(...),
    use_csv: str = Form("False"),
    email: str = Form(""),
):
    """
    Ejecuta la emisión real de badges para el lote indicado.
    - Lee config.json, image.png y (opcionalmente) recipients.csv de uploads/<batch_id>.
    - Actualiza las URLs públicas según la base del servidor.
    - Usa las funciones de generación para crear assertions y PNGs horneados.
    """
    batch_dir = UPLOADS_DIR / batch_id
    config_path = batch_dir / "config.json"
    image_path = batch_dir / "image.png"
    csv_path = batch_dir / "recipients.csv"

    if not config_path.is_file() or not image_path.is_file():
        body = f"""
        <h2>Error al ejecutar emisión</h2>
        <p>No se encontraron los ficheros necesarios para el lote <strong>{batch_id}</strong>.</p>
        <p><a href="/issuer">Volver al formulario</a></p>
        """
        return html_page(body)

    # Cargar config original
    with config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    # Ajustar la imagen en la config para que use la subida por web
    # Dejamos directory fijo ("badges") y copiamos la imagen allí si quieres,
    # pero para hornear ya estamos usando image_path, así que basta con guardar el nombre.
    config.setdefault("image", {})
    config["image"]["file"] = image_path.name
    config["image"]["directory"] = str(image_path.parent)

    # Construir URLs públicas desde la request
    base_url = str(request.base_url).rstrip("/")
    hosted_base_url = f"{base_url}/assertions" + "/{assertion_id}"
    image_public_base_url = f"{base_url}/badges-baked"

    # Determinar si usamos CSV o email único
    use_csv_flag = use_csv.lower() == "true"
    email = (email or "").strip()

    emitted = []  # lista de dicts con info de lo emitido

    # Asegurar directorio de salida
    OUTPUT_DIR = BASE_DIR / "output"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if use_csv_flag and csv_path.is_file():
        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            print("DEBUG csv fieldnames:", reader.fieldnames)
            for row in reader:
                print("DEBUG csv row:", row)
                recipient_email = (
                    row.get("email")
                    or row.get("Email")
                    or row.get("EMAIL")
                    or ""
                ).strip()
                print("DEBUG email extraído:", recipient_email)
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
                )
                ruta_json = guardar_assertion(assertion, OUTPUT_DIR, assertion_id)
                ruta_png = hornear_png(config, assertion, OUTPUT_DIR, assertion_id)
                emitted.append(
                    {
                        "email": recipient_email,
                        "assertion_id": assertion_id,
                        "json": str(ruta_json),
                        "png": str(ruta_png),
                    }
                )
    elif email:
            # Modo email único
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
            )
            ruta_json = guardar_assertion(assertion, OUTPUT_DIR, assertion_id)
            ruta_png = hornear_png(config, assertion, OUTPUT_DIR, assertion_id)
            emitted.append({
                "email": email,
                "assertion_id": assertion_id,
                "json": str(ruta_json),
                "png": str(ruta_png),
            })
    else:
        body = f"""
        <h2>Error al ejecutar emisión</h2>
        <p>Para el lote <strong>{batch_id}</strong> no se ha indicado ni CSV ni email válido.</p>
        <p><a href="/issuer">Volver al formulario</a></p>
        """
        return html_page(body)

    # Construir HTML de resumen
    body = f"""
    <h2>Emisión completada</h2>
    <p>ID de lote: <strong>{batch_id}</strong></p>
    <p>Se han emitido <strong>{len(emitted)}</strong> badges.</p>
    <table border="1" cellpadding="4" cellspacing="0">
      <tr>
        <th>Email</th>
        <th>Assertion ID</th>
        <th>Assertion URL</th>
        <th>Imagen PNG</th>
      </tr>
    """
    for item in emitted:
        assertion_id = item["assertion_id"]
        email_val = item["email"]
        assertion_url = f"{base_url}/assertions/{assertion_id}"
        image_url = f"{base_url}/badges-baked/{assertion_id}.png"
        body += f"""
      <tr>
        <td>{email_val}</td>
        <td>{assertion_id}</td>
        <td><a href="{assertion_url}" target="_blank">JSON</a></td>
        <td><a href="{image_url}" target="_blank">PNG</a></td>
      </tr>
        """

    body += """
    </table>
    <p><a href="/issuer">Emitir otro lote</a></p>
    <p><a href="/">Volver a la verificación</a></p>
    """
    return html_page(body)

@app.post("/verify-email-hash", response_class=JSONResponse)
async def verify_email_hash(assertion_id: str = Form(...), email: str = Form(...)):
    """
    Dado un assertion_id y un email:
    - Lee el assertion JSON.
    - Si recipient.hashed == true, recalcula el hash y compara.
    """
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

    # Reutiliza la misma lógica de hash que al emitir
    email_norm = email.strip().lower()
    to_hash = (salt + email_norm).encode("utf-8")
    digest = hashlib.sha256(to_hash).hexdigest()
    expected = f"sha256${digest}"

    match = (identity == expected)
    return JSONResponse(content={"ok": True, "match": match})

@app.post("/config/upload-json", response_class=JSONResponse)
async def upload_config_json(
    kind: str = Form(...),  # "issuer" o "badge"
    name: str = Form(...),
    file: UploadFile = File(...),
):
    """
    Sube un nuevo JSON de configuración:
    - kind: issuer / badge
    - name: base del nombre de fichero (sin extensión)
    """
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_").strip()
    if not safe_name:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Nombre inválido"})

    if kind == "issuer":
        filename = f"{safe_name}.issuer.json"
    elif kind == "badge":
        filename = f"{safe_name}.badge_class.json"
    else:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Tipo inválido"})

    target_path = CONFIG_DIR / filename
    contents = await file.read()
    try:
        data = json.loads(contents.decode("utf-8"))
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "JSON inválido"})

    with target_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return JSONResponse(content={"ok": True, "filename": filename})