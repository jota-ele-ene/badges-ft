import argparse
import csv
import json
import os
import uuid
import hashlib
import tempfile
import re
import shutil

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, unquote

from openbadges_bakery import bake

BASE_DIR = Path(__file__).resolve().parent


def cargar_configuracion(ruta_config):
    ruta = Path(ruta_config)
    if not ruta.is_file():
        raise FileNotFoundError(f"No se encontró el fichero de configuración: {ruta}")
    with ruta.open("r", encoding="utf-8") as f:
        return json.load(f)


def hash_email(email: str, salt: str | None = None) -> tuple[str, str | None]:
    email = email.strip().lower()
    if salt is None:
        salt = ""
    to_hash = (salt + email).encode("utf-8")
    digest = hashlib.sha256(to_hash).hexdigest()
    return f"sha256${digest}", salt


def limpiar_markdown_link(value: str) -> str:
    value = (value or "").strip()
    m = re.match(r"^\[(https?://[^\]]+)\]\((https?://[^\)]+)\)$", value)
    if m:
        return m.group(2).strip()
    return value


def construir_assertion(
    config,
    assertion_id,
    email,
    nombre=None,
    apellido1=None,
    apellido2=None,
    lang_override=None,
    hosted_base_url=None,
    image_public_base_url=None,
    batch_id: str | None = None,
):
    lang = lang_override or config.get("lang", "ES")
    issuer = config["issuer"]
    badge_class = config["badge_class"]
    verification_cfg = config.get("verification", {})

    hosted_base = hosted_base_url or verification_cfg.get("hosted_base_url", "").strip()
    if not hosted_base:
        raise ValueError("No se ha definido 'hosted_base_url' en la configuración ni por parámetro.")

    assertion_url = hosted_base.replace("{assertion_id}", assertion_id).strip()
    assertion_url = limpiar_markdown_link(assertion_url)

    identity, salt = hash_email(email)
    recipient = {
        "type": "email",
        "hashed": True,
        "identity": identity,
    }
    if salt:
        recipient["salt"] = salt

    issued_on = datetime.now(timezone.utc).isoformat()

    def get_lang_value(field):
        value = badge_class.get(field)
        if isinstance(value, dict):
            return value.get(lang) or value.get("EN") or next(iter(value.values()))
        return value

    badge_name = get_lang_value("name")
    badge_description = get_lang_value("description")
    badge_id = limpiar_markdown_link((badge_class.get("id") or "").strip())
    if not badge_id:
        raise ValueError("badge_class['id'] es obligatorio para construir la assertion.")

    verification = {
        "type": verification_cfg.get("type", "HostedBadge"),
        "url": assertion_url,
    }

    image_url = None
    if image_public_base_url:
        image_url = image_public_base_url.rstrip("/") + f"/{assertion_id}.png"

    assertion = {
        "@context": "https://w3id.org/openbadges/v2",
        "type": "Assertion",
        "id": assertion_url,
        "recipient": recipient,
        "badge": badge_id,
        "issuedOn": issued_on,
        "verification": verification,
    }

    if image_url:
        assertion["image"] = image_url

    full_name_parts = [p for p in [nombre, apellido1, apellido2] if p]
    if full_name_parts:
        assertion["name"] = " ".join(full_name_parts)
        assertion["description"] = f"Badge emitido a {assertion['name']} - {badge_name}"

    assertion["badgeName"] = badge_name
    assertion["badgeDescription"] = badge_description
    assertion["issuerName"] = issuer.get("name")

    if batch_id:
        assertion["batch"] = {"id": batch_id}

    return assertion


def guardar_assertion(assertion, output_dir, assertion_id):
    output_dir = Path(output_dir)
    assertions_dir = output_dir / "assertions"
    assertions_dir.mkdir(parents=True, exist_ok=True)

    ruta_json = assertions_dir / f"{assertion_id}.json"
    with ruta_json.open("w", encoding="utf-8") as f:
        json.dump(assertion, f, ensure_ascii=False, indent=2)

    return ruta_json


def _mapear_url_publica_a_ruta_local(url_o_path: str) -> Path | None:
    """
    Convierte una URL pública del proyecto en una ruta local del disco,
    ignorando scheme + host (BASE_URL).
    """
    value = limpiar_markdown_link(url_o_path or "").strip()
    if not value:
        return None

    parsed = urlparse(value)
    public_path = unquote(parsed.path or value)

    if public_path.startswith("/uploaded-badges/"):
        filename = public_path.removeprefix("/uploaded-badges/").lstrip("/")
        return BASE_DIR.parent / "uploads" / "badge_assets" / filename

    if public_path.startswith("/public-config/"):
        filename = public_path.removeprefix("/public-config/").lstrip("/")
        return BASE_DIR.parent / "config" / filename

    if public_path.startswith("/badges-baked/"):
        filename = public_path.removeprefix("/badges-baked/").lstrip("/")
        return BASE_DIR.parent / "output" / "badges_baked" / filename

    return None


def _resolver_imagen_base(config, output_dir):
    output_dir = Path(output_dir)
    image_cfg = config.get("image", {}) or {}

    image_dir = image_cfg.get("directory")
    image_file = image_cfg.get("file")
    if image_file:
        if image_dir:
            base_path = Path(image_dir) / image_file
        else:
            base_path = Path(image_file)

        if base_path.is_file():
            return base_path, None

        if not base_path.is_absolute():
            fallback_path = output_dir / base_path
            if fallback_path.is_file():
                return fallback_path, None

    badge_class = config.get("badge_class", {}) or {}
    badge_image = (badge_class.get("image") or "").strip()
    badge_image = limpiar_markdown_link(badge_image)
    if not badge_image:
        raise FileNotFoundError("No hay imagen en config['image'] ni en badge_class['image'].")

    local_from_url = _mapear_url_publica_a_ruta_local(badge_image)
    if local_from_url and local_from_url.is_file():
        return local_from_url, None

    base_path = Path(badge_image)
    if base_path.is_file():
        return base_path, None

    if not base_path.is_absolute():
        base_path = BASE_DIR / base_path

    if base_path.is_file():
        return base_path, None

    raise FileNotFoundError(
        f"No se encontró el PNG base localmente. "
        f"Valor recibido en badge_class['image']: {badge_image}"
    )


def hornear_png(config, assertion, output_dir, assertion_id):
    output_dir = Path(output_dir)
    baked_dir = output_dir / "badges_baked"
    baked_dir.mkdir(parents=True, exist_ok=True)
    baked_path = baked_dir / f"{assertion_id}.png"

    base_path, temp_path = _resolver_imagen_base(config, output_dir)

    if isinstance(assertion, dict):
        baked_payload = json.dumps(assertion, ensure_ascii=False, separators=(",", ":"))
    else:
        baked_payload = str(assertion)

    print(f"[bake] base_path={base_path}")
    print(f"[bake] payload=assertion JSON embebido (id={assertion.get('id') if isinstance(assertion, dict) else 'N/A'!r})")

    try:
        with base_path.open("rb") as input_file:
            baked_result = bake(input_file, baked_payload)

        if baked_result is None:
            print("[bake] WARNING: bake() devolvió None. Se copiará el PNG base sin hornear.")
            shutil.copyfile(base_path, baked_path)
            return baked_path

        if hasattr(baked_result, "read"):
            data = baked_result.read()
        else:
            data = baked_result

        if not data:
            print("[bake] WARNING: bake() no devolvió contenido. Se copiará el PNG base sin hornear.")
            shutil.copyfile(base_path, baked_path)
            return baked_path

        with baked_path.open("wb") as out:
            out.write(data)

    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass

    return baked_path


def procesar_desde_csv(args, config):
    csv_path = Path(args.csv)
    if not csv_path.is_file():
        raise FileNotFoundError(f"No se encontró el CSV: {csv_path}")

    batch_id = str(uuid.uuid4())

    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = row.get("email") or row.get("Email") or row.get("EMAIL")
            if not email:
                print("Fila sin email, se omite:", row)
                continue

            nombre = row.get("nombre") or row.get("Nombre")
            apellido1 = row.get("apellido1") or row.get("Apellido1")
            apellido2 = row.get("apellido2") or row.get("Apellido2")

            assertion_id = str(uuid.uuid4())
            assertion = construir_assertion(
                config=config,
                assertion_id=assertion_id,
                email=email.strip(),
                nombre=nombre,
                apellido1=apellido1,
                apellido2=apellido2,
                lang_override=args.lang,
                hosted_base_url=args.hosted_base_url,
                image_public_base_url=args.image_public_base_url,
                batch_id=batch_id,
            )
            ruta_json = guardar_assertion(assertion, args.output_dir, assertion_id)
            ruta_png = hornear_png(config, assertion, args.output_dir, assertion_id)
            print(f"Assertion generada para {email}: {ruta_json}")
            print(f"PNG horneado para {email}: {ruta_png}")


def procesar_individual(args, config):
    if not args.email:
        raise ValueError("Debe proporcionar --email en modo individual.")

    assertion_id = args.assertion_id or str(uuid.uuid4())
    batch_id = assertion_id

    assertion = construir_assertion(
        config=config,
        assertion_id=assertion_id,
        email=args.email.strip(),
        nombre=args.nombre,
        apellido1=args.apellido1,
        apellido2=args.apellido2,
        lang_override=args.lang,
        hosted_base_url=args.hosted_base_url,
        image_public_base_url=args.image_public_base_url,
        batch_id=batch_id,
    )
    ruta_json = guardar_assertion(assertion, args.output_dir, assertion_id)
    ruta_png = hornear_png(config, assertion, args.output_dir, assertion_id)
    print(f"Assertion generada para {args.email}: {ruta_json}")
    print(f"PNG horneado para {args.email}: {ruta_png}")
    print(f"ID del assertion: {assertion_id}")


def main():
    parser = argparse.ArgumentParser(
        description="Generador de Assertions Open Badges 2.0 con baking en PNG embebiendo el JSON completo."
    )
    parser.add_argument("--config", required=True, help="Ruta al fichero de configuración de emisión (JSON).")
    parser.add_argument("--csv", help="Ruta al CSV con columnas: nombre, apellido1, apellido2, email.")
    parser.add_argument("--email", help="Email del receptor (modo individual).")
    parser.add_argument("--nombre", help="Nombre del receptor (modo individual).")
    parser.add_argument("--apellido1", help="Primer apellido (modo individual).")
    parser.add_argument("--apellido2", help="Segundo apellido (modo individual).")
    parser.add_argument("--assertion-id", help="ID del assertion (si no se indica, se genera un UUID).")
    parser.add_argument("--lang", help="Código de idioma a usar (por ejemplo ES o EN). Si no se indica, se usa el del config.")
    parser.add_argument("--hosted-base-url", help="Sobrescribe verification.hosted_base_url del config (ej: https://dominio/assertions/{assertion_id}).")
    parser.add_argument("--image-public-base-url", help="Base URL pública desde la que se servirán las imágenes horneadas (ej: https://dominio/badges-baked).")
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("OPENBADGES_OUTPUT_DIR", "output"),
        help="Directorio de salida para assertions e imágenes (por defecto: output o $OPENBADGES_OUTPUT_DIR).",
    )

    args = parser.parse_args()
    config = cargar_configuracion(args.config)
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    if args.csv:
        procesar_desde_csv(args, config)
    else:
        procesar_individual(args, config)


if __name__ == "__main__":
    main()