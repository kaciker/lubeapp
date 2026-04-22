# LubeApp / FuelLog

Aplicación web ligera para registrar repostajes a partir de fotos del ticket y del odómetro. El frontend está hecho en una sola página HTML y la API en FastAPI. El flujo principal usa un modelo de visión vía OpenRouter para extraer los datos del repostaje, los guarda en SQLite y puede reenviarlos automáticamente a LubeLogger.

## Qué hace

- Captura una foto del ticket de combustible.
- Captura una foto del odómetro o permite escribir los kilómetros manualmente.
- Obtiene la ubicación GPS del dispositivo para ayudar a completar la gasolinera y la dirección.
- Envía las imágenes a OpenRouter y fuerza una respuesta JSON estructurada.
- Guarda cada análisis en SQLite.
- Muestra historial, permite borrar registros y exportar todo a JSON.
- Puede publicar automáticamente el repostaje en LubeLogger.
- Puede disparar un webhook con el JSON completo tras cada análisis.

## Cómo funciona

### Frontend

La interfaz principal vive en `index.html` y está pensada para móvil:

- Vista `Escanear` para tomar ticket y odómetro.
- Vista `Historial` para consultar repostajes guardados.
- Vista `Ajustes` para configurar vehículo, integración con LubeLogger y webhook.

Características visibles en la UI:

- Selección de modelo de visión.
- Soporte para GPS desde navegador.
- Compresión de imágenes en cliente antes de enviarlas al backend.
- Guardado local del nombre del vehículo y preferencias básicas en `localStorage`.
- Resultado del análisis en formato visual y JSON.

### Backend

La API está implementada en `api/main.py` con FastAPI y expone estas rutas:

- `GET /` sirve el frontend.
- `GET /api/health` comprueba estado general y si hay claves configuradas.
- `GET /api/lubelogger/vehicles` consulta vehículos en LubeLogger.
- `POST /api/analyze` analiza ticket y odómetro, persiste el resultado y opcionalmente lo envía a LubeLogger.
- `GET /api/records` devuelve el historial completo.
- `DELETE /api/records/{record_id}` borra un registro.
- `DELETE /api/records` borra todo el historial.
- `GET /api/export` exporta el histórico completo en JSON.
- `GET /api/config/{key}` y `PUT /api/config/{key}` guardan configuración simple en base de datos.

## Datos que extrae

El backend fuerza a la IA a devolver un JSON con esta estructura lógica:

- `fecha`
- `hora`
- `gasolinera`
- `direccion_gasolinera`
- `tipo_combustible`
- `litros`
- `precio_por_litro`
- `importe_total`
- `numero_ticket`
- `odometro_km`
- `vehiculo`
- `metodo_pago`
- `notas`

Además, el sistema añade metadatos internos como:

- `_id`
- `_timestamp`
- `_model`
- `_tokens`
- `_lat`
- `_lon`
- `_gps_address`
- `_lubelogger`

## Integración con OpenRouter

La app usa OpenRouter para enviar imágenes y texto a un modelo multimodal. Actualmente el backend solo permite estos modelos:

- `google/gemini-2.0-flash-001`
- `anthropic/claude-3-5-haiku`
- `anthropic/claude-sonnet-4-5`
- `openai/gpt-4o-mini`

El prompt obliga a devolver exclusivamente JSON para que el backend pueda validarlo y persistirlo.

## Integración con LubeLogger

Si se configuran `LUBELOGGER_URL`, `LUBELOGGER_API_KEY` y el `vehicleId` en ajustes:

- consulta la lista de vehículos disponibles;
- sube la foto del ticket como documento;
- crea un registro de combustible en LubeLogger;
- adjunta coordenadas GPS como `extraField` llamado `gps` si existen.

Si la integración no está configurada, el análisis sigue funcionando y el resultado queda guardado localmente.

## Webhook de salida

Opcionalmente, la app puede enviar un `POST` con el JSON completo del análisis a una URL externa configurada desde ajustes. Los errores del webhook no bloquean el flujo principal.

## Estructura del proyecto

```text
.
├── api/
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── data/
│   └── lubeapp.db
├── .env
├── .env.example
├── docker-compose.yml
├── fuel-tracker.html
└── index.html
```

## Variables de entorno

Ejemplo base:

```env
OPENROUTER_API_KEY=sk-or-v1-...
LUBELOGGER_URL=http://host-o-ip:8082
LUBELOGGER_API_KEY=
```

Variables relevantes:

- `OPENROUTER_API_KEY`: obligatoria para analizar imágenes.
- `LUBELOGGER_URL`: opcional, activa la integración con LubeLogger.
- `LUBELOGGER_API_KEY`: opcional, necesaria para consultar vehículos y subir repostajes a LubeLogger.

## Puesta en marcha con Docker

El `docker-compose.yml` actual levanta un único servicio en el puerto `8087` y monta:

- `./data` en `/data` para persistencia SQLite.
- `./index.html` en `/app/index.html` como volumen de solo lectura.

Arranque:

```bash
docker compose up --build -d
```

Luego la app queda disponible en:

```text
http://localhost:8087
```

## Desarrollo local sin Docker

Requisitos:

- Python 3.12 o compatible.

Instalación:

```bash
cd api
pip install -r requirements.txt
cp ../.env.example ../.env
```

Ejecución:

```bash
cd /opt/lubeapp
uvicorn api.main:app --host 0.0.0.0 --port 8087
```

Nota: el `Dockerfile` copia `index.html` dentro de `/app`, mientras que en desarrollo local el backend espera servir ese archivo desde el mismo árbol del proyecto.

## Persistencia

La base de datos SQLite se guarda en:

```text
/data/lubeapp.db
```

Se crean dos tablas principales:

- `records`: histórico completo de análisis.
- `config`: ajustes simples como webhook y vehículo de LubeLogger.

## Seguridad y publicación en GitHub

Antes de subir este proyecto a GitHub conviene no versionar:

- `.env`
- `data/lubeapp.db`
- cualquier otro dato local generado en tiempo de ejecución

El repositorio debe incluir `.env.example`, pero no claves reales ni datos privados.

## Observaciones técnicas

- La API permite CORS abierto (`*`).
- El frontend comprime imágenes en cliente a un máximo aproximado de `1280px`.
- El backend limita el tamaño de cada imagen enviada en base64.
- Si no hay foto del odómetro, acepta kilometraje manual.
- La geocodificación inversa se hace contra Nominatim de OpenStreetMap.


