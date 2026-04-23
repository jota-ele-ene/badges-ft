# Dockerfile para ejecutar la app Open Badges FT en Debian (python:3.12-slim)

FROM python:3.12-slim

# Variables de entorno recomendadas
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Directorio de trabajo dentro del contenedor
WORKDIR /app

# Instalar paquetes básicos del sistema (puedes ampliar según tus necesidades)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalar dependencias Python
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && \
    pip install -r /app/requirements.txt

# Copiar el código de la aplicación
COPY . /app

# Crear directorios necesarios para output y uploads
RUN mkdir -p /app/output/assertions /app/output/badges_baked /app/uploads /app/config

# Exponer el puerto donde correrá Uvicorn
EXPOSE 8000

# Comando por defecto: lanzar FastAPI con Uvicorn
CMD ["uvicorn", "verification_app.main:app", "--host", "0.0.0.0", "--port", "8000"]