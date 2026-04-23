import argparse
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import uuid

from openbadges_bakery import bake  # nueva dependencia


def cargar_configuracion(ruta_config):
    ruta = Path(ruta_config)
    if not ruta.is_file():
        raise FileNotFoundError(f"No se encontró el fichero de configuración: {ruta}")
    with ruta.open("r", encoding="utf-8") as f:
        return json.load(f)

import hashlib

def hash_email(email: str, salt: str | None = None) -> tuple[str, str | None]:
    """
    Devuelve (identity, salt) para recipient:
    - identity: 'sha256$<hash>'
    - salt: la sal usada (o None)
    """
    email = email.strip().lower()
    if salt is None:
        # Podrías generar una sal fija por emisión o por persona, aquí simple:
        salt = ""
    to_hash = (salt + email).encode("utf-8")
    digest = hashlib.sha256(to_hash).hexdigest()
    return f"sha256${digest}", salt

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
):
    # Idioma
    lang = lang_override or config.get("lang", "ES")

    issuer = config["issuer"]
    badge_class = config["badge_class"]
    verification_cfg = config.get("verification", {})

    # hosted_base_url desde config o parámetro
    hosted_base = (
        hosted_base_url
        or verification_cfg.get("hosted_base_url", "").strip()
    )
    if not hosted_base:
        raise ValueError("No se ha definido 'hosted_base_url' en la configuración ni por parámetro.")

    # Construir URL del assertion
    assertion_url = hosted_base.replace("{assertion_id}", assertion_id)

    identity, salt = hash_email(email)
    recipient = {
        "type": "email",
        "hashed": True,
        "identity": identity
    }
    if salt:
        recipient["salt"] = salt

    # Fecha de emisión en formato ISO 8601
    issued_on = datetime.now(timezone.utc).isoformat()

    # Selección de nombre/descr según idioma
    def get_lang_value(field):
        value = badge_class.get(field)
        if isinstance(value, dict):
            return value.get(lang) or value.get("EN") or next(iter(value.values()))
        return value

    badge_name = get_lang_value("name")
    badge_description = get_lang_value("description")

    badge_id = badge_class["id"]

    verification = {
        "type": verification_cfg.get("type", "HostedBadge"),
        "url": assertion_url,
    }

    # URL pública de la imagen horneada
    if image_public_base_url:
        image_url = image_public_base_url.rstrip("/") + f"/{assertion_id}.png"
    else:
        image_url = None

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

    # Campos opcionales con nombre completo
    full_name_parts = [p for p in [nombre, apellido1, apellido2] if p]
    if full_name_parts:
        assertion["name"] = " ".join(full_name_parts)
        assertion["description"] = f"Badge emitido a {assertion['name']} - {badge_name}"

    # Metadatos de display
    assertion["badgeName"] = badge_name
    assertion["badgeDescription"] = badge_description
    assertion["issuerName"] = issuer.get("name")

    return assertion


def guardar_assertion(assertion, output_dir, assertion_id):
    output_dir = Path(output_dir)
    assertions_dir = output_dir / "assertions"
    assertions_dir.mkdir(parents=True, exist_ok=True)

    ruta_json = assertions_dir / f"{assertion_id}.json"
    with ruta_json.open("w", encoding="utf-8") as f:
        json.dump(assertion, f, ensure_ascii=False, indent=2)

    return ruta_json


def hornear_png(config, assertion, output_dir, assertion_id):
    """
    Lee el PNG base indicado en el config y hornea en él el JSON de la assertion.
    Guarda el resultado en output/badges_baked/<assertion_id>.png
    """
    image_cfg = config.get("image", {})
    image_dir = image_cfg.get("directory", "badges")
    image_file = image_cfg.get("file", "acme_default.png")

    base_path = Path(image_dir) / image_file
    if not base_path.is_file():
        raise FileNotFoundError(f"No se encontró el PNG base: {base_path}")

    output_dir = Path(output_dir)
    baked_dir = output_dir / "badges_baked"
    baked_dir.mkdir(parents=True, exist_ok=True)

    baked_path = baked_dir / f"{assertion_id}.png"

    assertion_json_str = json.dumps(assertion, ensure_ascii=False)

    with base_path.open("rb") as input_file:
        baked_tmp = bake(input_file, assertion_json_str)
        # baked_tmp es un archivo temporal; copiamos su contenido al destino
        with baked_path.open("wb") as out:
            out.write(baked_tmp.read())

    return baked_path


def procesar_desde_csv(args, config):
    csv_path = Path(args.csv)
    if not csv_path.is_file():
        raise FileNotFoundError(f"No se encontró el CSV: {csv_path}")

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
            )
            ruta_json = guardar_assertion(assertion, args.output_dir, assertion_id)
            ruta_png = hornear_png(config, assertion, args.output_dir, assertion_id)
            print(f"Assertion generada para {email}: {ruta_json}")
            print(f"PNG horneado para {email}: {ruta_png}")


def procesar_individual(args, config):
    if not args.email:
        raise ValueError("Debe proporcionar --email en modo individual.")

    assertion_id = args.assertion_id or str(uuid.uuid4())
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
    )
    ruta_json = guardar_assertion(assertion, args.output_dir, assertion_id)
    ruta_png = hornear_png(config, assertion, args.output_dir, assertion_id)
    print(f"Assertion generada para {args.email}: {ruta_json}")
    print(f"PNG horneado para {args.email}: {ruta_png}")
    print(f"ID del assertion: {assertion_id}")


def main():
    parser = argparse.ArgumentParser(
        description="Generador de Assertions Open Badges 2.0 (texto plano, hosted) con baking en PNG."
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Ruta al fichero de configuración de emisión (JSON).",
    )
    parser.add_argument(
        "--csv",
        help="Ruta al CSV con columnas: nombre, apellido1, apellido2, email.",
    )
    parser.add_argument(
        "--email",
        help="Email del receptor (modo individual).",
    )
    parser.add_argument("--nombre", help="Nombre del receptor (modo individual).")
    parser.add_argument("--apellido1", help="Primer apellido (modo individual).")
    parser.add_argument("--apellido2", help="Segundo apellido (modo individual).")
    parser.add_argument(
        "--assertion-id",
        help="ID del assertion (si no se indica, se genera un UUID).",
    )
    parser.add_argument(
        "--lang",
        help="Código de idioma a usar (por ejemplo ES o EN). Si no se indica, se usa el del config.",
    )
    parser.add_argument(
        "--hosted-base-url",
        help="Sobrescribe verification.hosted_base_url del config (ej: https://dominio/assertions/{assertion_id}).",
    )
    parser.add_argument(
        "--image-public-base-url",
        help="Base URL pública desde la que se servirán las imágenes horneadas (ej: https://dominio/badges-baked).",
    )
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