FROM python:3.12.15-slim-bookworm AS backend
RUN pip install --no-cache-dir uv==0.12.23
WORKDIR /app
COPY . .
RUN uv sync --locked --no-dev --no-editable && .venv/bin/python scripts/export_web_contract.py
FROM node:22.22.0-bookworm-slim AS web
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
COPY --from=backend /app/web/openapi.json ./openapi.json
RUN npx openapi-typescript openapi.json -o src/api-schema.d.ts && npm run typecheck && npx vite build
FROM backend
COPY --from=web /build/web/dist /app/web/dist
RUN useradd --uid 10001 --create-home jobagent && chown -R jobagent:jobagent /app
USER jobagent
EXPOSE 8001
CMD ["sh","-c",".venv/bin/python -m database.migrate && exec .venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8001 --workers 1 --no-proxy-headers --timeout-graceful-shutdown 30"]
