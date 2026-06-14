# JobPilot — Backend

> API service for JobPilot: resume tailoring, job search, application autofill,
> interview feedback, and tech-chronicle. Part of the
> [Aithrex](https://aithrex.tech) suite.

Standalone copy of the `jobs-resume` microservice extracted from the portfolio
monorepo. It serves the resume tailoring, job search, application autofill,
feedback, and tech-chronicle APIs.

> **Status:** initial copy. The portfolio monorepo still runs the original;
> this repo is where the service is hardened independently. Once stable, the
> copy in portfolio will be retired.

## Layout

```
app.py              Flask app factory (registers blueprints, CORS, JWT)
lambda_handler.py   AWS Lambda entrypoint (API Gateway -> apig-wsgi -> Flask)
run_local.py        Local dev runner (wires shared/python onto sys.path)
blueprints/         jobs, resume, apply, feedback, tech_chronicle
services/           business logic (resume tailoring, scoring, LLM clients, …)
schemas/            request/response schemas
shared/python/      shared utils.* and models.* (db_connect, config, security, …)
requirements.txt
```

## Auth model

This service **validates** JWTs; it does not issue them. Users still log in
through the existing **portfolio auth service**. The only requirement is that
`JWT_SECRET_KEY` here is identical to the auth service's — see `.env.example`.

## Local development

```bash
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
cp .env.example .env        # then fill in JWT_SECRET_KEY, MONGODB_URI, keys
python run_local.py
```

API is then at `http://localhost:5000/api/*` (health: `/api/health`).

## Deployment (later)

Deploys as an AWS Lambda behind API Gateway via `lambda_handler.handler`.
When packaging, the `shared/python` directory must be importable — either
bundled into the zip or published as a Lambda layer mounted at `/opt/python`
(the original portfolio used a shared layer). Terraform for the new Aithrex
stack (Lambda + API Gateway route + S3 resume bucket) is still to be created.
