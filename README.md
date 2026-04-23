# Open Badges FT – Emisor y Verificador 2.0

Aplicación web basada en **FastAPI** para **emitir** y **verificar** insignias digitales compatibles con **Open Badges 2.0** mediante assertions alojadas (HostedBadge) y PNG horneados. [web:213][web:214]

Permite:
- Verificar badges a partir de una **URL de assertion** o de un **PNG horneado**.
- Emitir badges para **uno o varios receptores** (email único o CSV).
- Generar **Assertions 2.0 hosted** (JSON) y **PNG horneados** listos para validadores externos.
- Gestionar **Issuer** y **BadgeClass** mediante ficheros JSON reutilizables en el directorio `config/`.

---

## Árbol de directorios

```text
badges-ft/
├── config/
│   ├── acme.issuer.json          # Definiciones de Issuer (Open Badges 2.0)
│   ├── python.badge_class.json   # Definiciones de BadgeClass (Open Badges 2.0)
│   └── ...                       # Otros JSON de configuración
├── output/
│   ├── assertions/               # Assertions generadas (JSON por receptor)
│   └── badges_baked/             # Imágenes PNG horneadas por assertion
├── uploads/
│   └── <batch_id>/               # Lotes de emisión gestionados por la interfaz web
│       ├── config.json           # Config efectiva usada en el lote
│       ├── recipients.csv        # CSV de receptores del lote (opcional)
│       └── image.png             # Imagen base subida para el lote
├── verification_app/
│   ├── __init__.py
│   └── main.py                   # Aplicación FastAPI (verificación y emisión)
├── generate_badges.py            # Construcción de assertions y horneado de PNG
├── requirements.txt              # Dependencias Python (FastAPI, Uvicorn, etc.)
└── README.md                     # Este fichero
```

---

## Instalación local (sin Docker)

### 1. Obtener el código

```bash
mkdir -p /opt/badges-ft
cd /opt/badges-ft
# O bien descarga el ZIP y descomprímelo aquí
git clone <URL_DEL_REPO> .
```

### 2. Crear entorno virtual

Se recomienda Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate      # Linux/macOS
# En Windows (PowerShell):
# .venv\Scripts\Activate.ps1
```

### 3. Instalar dependencias

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

Si no hubiera `requirements.txt`, lo mínimo es:

```bash
pip install "fastapi" "uvicorn[standard]"
```

### 4. Crear directorios de trabajo

```bash
mkdir -p config
mkdir -p output/assertions output/badges_baked
mkdir -p uploads
```

---

## Puesta en marcha local

Desde la raíz del proyecto:

```bash
uvicorn verification_app.main:app --host 0.0.0.0 --port 8000
```

La aplicación quedará disponible en `http://localhost:8000` y podrás acceder a la UI de verificación y emisión. [web:194]

---

## Uso de la aplicación

### Verificación de badges

- URL base: `http://localhost:8000/`

Funciones:
- **Verificar por URL de assertion**: se introduce la URL HTTP donde está alojado el JSON de la Assertion 2.0; se comprueba estructura, tipo `Assertion` y verificación HostedBadge. [web:213][web:220]
- **Verificar por PNG horneado**: se sube un `.png` con metadatos Open Badges embebidos; se extrae la assertion y se contrasta con la versión alojada si aplica. [web:214]

### Emisión de badges

- URL de emisión: `http://localhost:8000/issuer`

Flujo:
1. **Seleccionar Issuer y BadgeClass** desde los JSON que haya en `config/`.
2. **Definir receptores**: email único o fichero `recipients.csv` con cabecera que incluya columna `email`.
3. **Subir imagen base** del badge (PNG).
4. **Previsualizar lote** (`/issuer/preview`): se crea un `batch_id` y carpeta en `uploads/<batch_id>/` con `config.json`, `image.png` y `recipients.csv`.
5. **Ejecutar emisión** (`/issuer/exec`): se generan assertions Open Badges 2.0 y PNG horneados en `output/assertions/` y `output/badges_baked/`.

Cada receptor obtiene:
- Una Assertion 2.0 alojada (HostedBadge) accesible vía `/assertions/<assertion_id>.json`.
- Un PNG horneado accesible vía `/badges-baked/<assertion_id>.png`. [web:213]

---

## Configuración de Issuer y BadgeClass

### Issuer (`config/*.issuer.json`)

Ejemplo:

```json
{
  "@context": "https://w3id.org/openbadges/v2",
  "id": "https://TU_DOMINIO/issuer/acme.issuer.json",
  "type": "Issuer",
  "name": "ACME S.A.",
  "url": "https://acme.example.org",
  "email": "badges@acme.example.org",
  "description": "Organización ficticia utilizada como emisor de badges de ejemplo."
}
```

- `id` debe ser una URL estable y pública.
- `name`, `url` y `email` identifican a la organización emisora. [web:214]

### BadgeClass (`config/*.badge_class.json`)

Ejemplo:

```json
{
  "@context": "https://w3id.org/openbadges/v2",
  "id": "https://TU_DOMINIO/badges/python.badge_class.json",
  "type": "BadgeClass",
  "name": {
    "ES": "Introducción a Python",
    "EN": "Introduction to Python"
  },
  "description": {
    "ES": "Badge emitido por ACME por completar un curso introductorio de Python.",
    "EN": "Badge issued by ACME for completing an introductory Python course."
  },
  "image": "https://TU_DOMINIO/badges/images/python.png",
  "criteria": {
    "id": "https://acme.example.org/badges/python/criteria.html"
  },
  "issuer": "https://TU_DOMINIO/issuer/acme.issuer.json"
}
```

- `issuer` apunta al Issuer definido arriba.
- La Assertion usará este `id` en el campo `badge`. [web:214]

---

## Despliegue con Docker (Debian)

El proyecto incluye un `Dockerfile` basado en `python:3.12-slim` (Debian) para ejecutar la aplicación en contenedor de forma sencilla. [web:194][web:216]

### 1. Dockerfile

Guarda esto como `Dockerfile` en la raíz:

```dockerfile
# Dockerfile para ejecutar la app Open Badges FT en Debian (python:3.12-slim)

FROM python:3.12-slim

# Variables de entorno recomendadas
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Directorio de trabajo dentro del contenedor
WORKDIR /app

# Instalar paquetes básicos del sistema (amplía si necesitas libs adicionales)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalar dependencias Python
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Copiar el código de la aplicación
COPY . /app

# Crear directorios necesarios para output y uploads
RUN mkdir -p /app/output/assertions /app/output/badges_baked /app/uploads /app/config

# Exponer el puerto donde correrá Uvicorn
EXPOSE 8000

# Comando por defecto: lanzar FastAPI con Uvicorn
CMD ["uvicorn", "verification_app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 2. Construir la imagen

```bash
docker build -t openbadges-ft-debian .
```

Esto crea una imagen con todas las dependencias listas para ejecutar la app mediante Uvicorn. [web:209][web:216]

### 3. Ejecutar el contenedor

```bash
docker run --rm -it \
  -p 8000:8000 \
  -v "$(pwd)/config:/app/config" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/uploads:/app/uploads" \
  openbadges-ft-debian
```

- `-p 8000:8000` expone la API en `http://localhost:8000`.
- Los `-v` montan las carpetas locales para persistir configuración (`config`), assertions y PNG horneados (`output`) y lotes (`uploads`). [web:199][web:202]

Con el contenedor arrancado:
- `http://localhost:8000/` → verificación.
- `http://localhost:8000/issuer` → emisión.

---

## Notas de despliegue

- Limpia periódicamente `output/` y `uploads/` si generas muchos lotes.
- Si se despliega detrás de un proxy (por ejemplo, Nginx o Traefik), ajusta:
  - Puerto y host de Uvicorn.
  - Las URLs en `Issuer`, `BadgeClass` y `Assertion` para usar el dominio público del proxy. [web:200][web:218]

---

## Detalle técnico: verificación de recipient (email hashed)

Este proyecto utiliza el esquema estándar de **recipient hasheado** de Open Badges 2.0 para representar el email del receptor en la Assertion. [web:214][web:222]

### Formato del recipient en la Assertion

Cuando se emite un badge, en el JSON de la Assertion se incluye un bloque `recipient` como este:

```json
"recipient": {
  "type": "email",
  "hashed": true,
  "identity": "sha256<...>"
}
```

- `type`: se usa `email` para indicar que la identidad del receptor es una dirección de correo electrónico. [web:214]  
- `hashed: true`: indica que el valor de `identity` es un hash, no el email en texto plano.  
- `identity`: contiene el hash del email en formato `sha256$<hash_hex>`, donde `<hash_hex>` es el resultado de aplicar SHA-256 al email normalizado. [web:225]

En la implementación actual **no se utiliza `salt`**, por lo que no aparece el campo `salt` en el JSON del recipient. Según la especificación, si `salt` no está presente, se asume que el hash se ha calculado sin sal. [web:214]

### Cómo se calcula el hash del email

Al emitir un badge:

1. El email se normaliza a minúsculas y se recortan espacios en blanco.
2. Se aplica SHA-256 al email normalizado (sin sal).
3. Se construye la `identity` como:

   ```text
   identity = "sha256$" + sha256(email_normalizado)
   ```

En pseudocódigo:

```python
email_norm = email.strip().lower()
digest = sha256(email_norm.encode("utf-8")).hexdigest()
identity = f"sha256${digest}"
```

Este valor se guarda en `recipient.identity` junto con `hashed: true`. [web:214][web:225]

### Cómo se verifica el email del receptor

La interfaz de verificación permite introducir un email para comprobar si coincide con el recipient del badge:

1. Se recupera la Assertion correspondiente al `assertion_id` que se está inspeccionando.
2. Se extraen `recipient.identity` (y `recipient.salt` si existiera).
3. Se normaliza el email introducido (minúsculas, sin espacios).
4. Se recalcula el hash usando la misma lógica que en la emisión.
5. Se compara el valor calculado con `recipient.identity`:
   - Si coinciden, se muestra el mensaje “El email coincide con el hash del badge”.
   - Si no coinciden, se indica que el email no coincide.

Este mecanismo permite comprobar que un badge pertenece a un email concreto **sin exponer el correo en texto plano** en el JSON público de la Assertion, siguiendo las recomendaciones de la especificación Open Badges 2.0 sobre hashing del recipient. [web:214][web:222]