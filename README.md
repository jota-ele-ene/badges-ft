# Open Badges FT – Emisor y Verificador 2.0

Aplicación web basada en **FastAPI** para **emitir** y **verificar** insignias digitales compatibles con **Open Badges 2.0** mediante assertions alojadas (HostedBadge) y PNG horneados. [1][2]

Permite:
- Verificar badges a partir de una **URL de assertion** o de un **PNG horneado**. [1][2]
- Emitir badges para **uno o varios receptores** (email único o CSV). [3]
- Generar **Assertions 2.0 hosted** (JSON) y **PNG horneados** listos para validadores externos. [1][2]
- Gestionar **Issuer** y **BadgeClass** mediante ficheros JSON reutilizables en el directorio `config/`. [3]
- Proteger rutas privadas con **JWT** en la versión actualizada del backend, por lo que es necesario instalar `PyJWT` si se usa el `main.py` actualizado. 

***

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
│   └── main.py                   # Aplicación FastAPI (verificación, emisión y login JWT)
├── static/
│   ├── footer.html               # Footer que carga la protección cliente
│   └── auth-guard.js             # JS que propaga token y redirige a /login
├── generate_badges.py            # Construcción de assertions y horneado de PNG
├── requirements.txt              # Dependencias Python
└── README.md                     # Este fichero
```

***

## Instalación local (sin Docker)

### 1. Obtener el código

```bash
mkdir -p /opt/badges-ft
cd /opt/badges-ft
# O bien descarga el ZIP y descomprímelo aquí
git clone <URL_DEL_REPO> .
```

### 2. Crear entorno virtual

Se recomienda Python 3.11+ para la ejecución local. [4][5]

```bash
python -m venv .venv
source .venv/bin/activate      # Linux/macOS
# En Windows (PowerShell):
# .venv\Scripts\Activate.ps1
```


### 3. Instalar dependencias:

```bash
python -m pip install -r requirements.txt
```

## requirements.txt

El archivo `requirements.txt` debe estar en la raíz del proyecto y contener:

```txt
fastapi
uvicorn[standard]
python-multipart
PyJWT
python-dotenv
openbadges-bakery
Pillow
```


### 4. Crear directorios de trabajo

```bash
mkdir -p config
mkdir -p output/assertions output/badges_baked
mkdir -p uploads
mkdir -p static
```

### 5. Colocar los archivos estáticos del guard

El archivo `auth-guard.js` debe colocarse en `static/auth-guard.js`, y el `footer.html` debe estar en `static/footer.html`, porque la aplicación monta `/static` desde esa carpeta y el footer carga el script desde `src="/static/auth-guard.js"`. 

***

## Puesta en marcha local

Desde la raíz del proyecto:

```bash
uvicorn verification_app.main:app --host 0.0.0.0 --port 8000
```

La aplicación quedará disponible en `http://localhost:8000`. Uvicorn es una forma estándar de servir apps FastAPI, y FastAPI documenta este patrón de despliegue también en contenedores. [4][5]

***

## Uso de la aplicación

### Verificación de badges

- URL base: `http://localhost:8000/`

Funciones:
- **Verificar por ID / assertion alojada**: la verificación hosted en Open Badges 2.0 se basa en una Assertion HTTP(S) con tipo `HostedBadge`, validada a partir del `id` de la assertion. [1]
- **Verificar por PNG horneado**: se sube un `.png` con metadatos Open Badges embebidos; el badge baking permite extraer la assertion desde la imagen y verificarla. [2][6]

### Login y protección de rutas

- URL de login: `http://localhost:8000/login`

Flujo:
1. El usuario introduce su correo en `/login`. 
2. El backend genera un JWT con `email` y `tkn`, donde `tkn` es el hash SHA-256 del correo normalizado. 
3. Todas las rutas salvo `/`, `/login`, `/verify-id` y `/verify-png` redirigen a `/login` si no reciben un token válido. 
4. El archivo `static/auth-guard.js` añade el token a enlaces, formularios y llamadas `fetch`, y redirige también al login en cliente si no encuentra un token coherente. 

### Emisión de badges

- URL de emisión: `http://localhost:8000/issuer`

Flujo:
1. **Seleccionar Issuer y BadgeClass** desde los JSON que haya en `config/`. [3]
2. **Definir receptores**: email único o fichero `recipients.csv` con cabecera que incluya columna `email`. [3]
3. **Subir imagen base** del badge (PNG). [3]
4. **Previsualizar lote** (`/issuer/preview`): se crea un `batch_id` y una carpeta en `uploads/<batch_id>/` con `config.json`, `image.png` y `recipients.csv` si aplica. [3]
5. **Ejecutar emisión** (`/issuer/exec`): se generan assertions Open Badges 2.0 y PNG horneados en `output/assertions/` y `output/badges_baked/`. [3]

Cada receptor obtiene:
- Una Assertion 2.0 alojada accesible vía `/assertions/<assertion_id>`. [1]
- Un PNG horneado accesible vía `/badges-baked/<assertion_id>.png` o nombre equivalente generado en salida. [2]

***

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

- `id` debe ser una URL estable y pública. [1]
- `name`, `url` y `email` identifican a la organización emisora. [1]

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

- `issuer` apunta al Issuer definido arriba. [1]
- La Assertion usará este `id` en el campo `badge`. [1]

***

## Despliegue con Docker (Debian)

Un despliegue típico de FastAPI con Docker usa una imagen slim de Python, copia `requirements.txt`, instala dependencias y arranca Uvicorn con el módulo ASGI de la aplicación. [7][4][5]

### 1. Dockerfile

Guarda esto como `Dockerfile` en la raíz:

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends     curl     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip &&     pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN mkdir -p /app/output/assertions /app/output/badges_baked /app/uploads /app/config /app/static

EXPOSE 8000

CMD ["uvicorn", "verification_app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Si usas el backend con JWT, asegúrate de que `requirements.txt` incluye `PyJWT>=2.8.0`, porque el módulo `verification_app.main` importa `jwt`. 

### 2. Construir la imagen

```bash
docker build -t openbadges-ft-debian .
```

Esto crea una imagen con el patrón habitual de despliegue de FastAPI en contenedores ligeros de Python. [7][5]

### 3. Ejecutar el contenedor

```bash
docker run --rm -it   -p 8000:8000   -v "$(pwd)/config:/app/config"   -v "$(pwd)/output:/app/output"   -v "$(pwd)/uploads:/app/uploads"   -v "$(pwd)/static:/app/static"   openbadges-ft-debian
```

- `-p 8000:8000` expone la app en `http://localhost:8000`. [7][5]
- Los `-v` montan configuración, resultados, lotes y ficheros estáticos para persistencia. 

Con el contenedor arrancado:
- `http://localhost:8000/` → verificación. 
- `http://localhost:8000/login` → acceso JWT. 
- `http://localhost:8000/issuer` → emisión, redirigida a login si no hay token válido. 

***

## Notas de despliegue

- Limpia periódicamente `output/` y `uploads/` si generas muchos lotes. [3]
- Si se despliega detrás de un proxy, revisa las URLs públicas usadas en `Issuer`, `BadgeClass` y Assertions alojadas para que apunten al dominio real. La verificación hosted en Open Badges 2.0 depende de URLs HTTP(S) accesibles y coherentes. [1]
- Configura `JWT_SECRET` como variable de entorno en producción para no depender del valor por defecto del backend. 

***

## Detalle técnico: verificación de recipient (email hashed)

Open Badges 2.0 permite representar la identidad del receptor mediante un `recipient` hasheado para evitar exponer el email en texto plano. [8][1]

### Formato del recipient en la Assertion

Cuando se emite un badge, el JSON de la Assertion incluye un bloque `recipient` como este:

```json
"recipient": {
  "type": "email",
  "hashed": true,
  "identity": "sha256$<hash_hex>"
}
```

- `type`: se usa `email` para indicar que la identidad del receptor es una dirección de correo electrónico. [1]
- `hashed: true`: indica que el valor de `identity` es un hash. [1]
- `identity`: contiene el hash del email en formato `sha256$<hash_hex>`. [9][1]

Si `salt` no está presente, la especificación indica que debe asumirse que el hash no está salado. [9][1]

### Cómo se calcula el hash del email

Al emitir un badge:

1. El email se normaliza a minúsculas y se recortan espacios en blanco. [3]
2. Se aplica SHA-256 al email normalizado. [3]
3. Se construye `identity` como:

```text
identity = "sha256$" + sha256(email_normalizado)
```

En pseudocódigo:

```python
email_norm = email.strip().lower()
digest = sha256(email_norm.encode("utf-8")).hexdigest()
identity = f"sha256${digest}"
```

Este valor se guarda en `recipient.identity` junto con `hashed: true`. [3][8][1]

### Cómo se verifica el email del receptor

La interfaz de verificación permite introducir un email para comprobar si coincide con el recipient del badge. [3]

1. Se recupera la Assertion correspondiente al `assertion_id`. [3]
2. Se extraen `recipient.identity` y `recipient.salt` si existe. [3][1]
3. Se normaliza el email introducido. [3]
4. Se recalcula el hash con la misma lógica. [3]
5. Se compara el valor calculado con `recipient.identity`; si coinciden, la interfaz indica que el email corresponde al badge. [3][8]

Este mecanismo permite comprobar la pertenencia del badge a un email concreto sin exponer el correo en texto plano en la Assertion pública. [8][1]