# Common tasks. Run from the repo root.
PY ?= python3
DB ?= sqlite:///./data/demo.db
RUN = cd backend && DATABASE_URL=$(DB) DATA_DIR=./data

.PHONY: install web demo seed api worker dev-web test e2e docker

install:          ## Python and web dependencies (and the Chromium Playwright uses for the browser checks)
	cd backend && $(PY) -m pip install -r requirements-dev.txt
	cd web && npm ci
	$(PY) -m playwright install chromium

web:              ## build the web app into web/dist (the API serves it)
	cd web && npm run build

seed:             ## wipe the demo database and load the synthetic hospital
	cd backend && rm -f data/demo.db
	$(RUN) $(PY) -m app.seed --reset

demo: web seed    ## build, reseed, and run everything on http://127.0.0.1:8000
	$(RUN) $(PY) -m uvicorn app.main:app --port 8000

api:              ## API with reload, against the existing demo database
	$(RUN) $(PY) -m uvicorn app.main:app --port 8000 --reload

worker:           ## the background sweep (windows, letters, digest), every 60 s
	$(RUN) $(PY) -m app.worker

dev-web:          ## web app with hot reload on :5173 (proxies /api to :8000)
	cd web && npm run dev

test:             ## backend tests (SQLite). For Postgres: DATABASE_URL=postgresql+psycopg://... make test
	cd backend && $(PY) -m pytest -q

e2e:              ## browser checks; needs `make demo` running and freshly seeded
	$(PY) tools/extension_e2e.py http://127.0.0.1:8000 screens
	$(PY) tools/screens.py http://127.0.0.1:8000 screens

docker:           ## Postgres + API + worker (fill .env first)
	docker compose up --build
