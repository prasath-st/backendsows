# backends/

Role-based backends, separated into one folder per role. Each role's FastAPI
service boots standalone on its own port and shares the same Neon Postgres DB.

## Layout

```
backends/
  mentor/backend/        → uvicorn app:app  (mentor_app + auth_app + shared)
  super-admin/backend/   → uvicorn app:app  (superadmin_app + auth_app + shared)
  enterprise/backend/    → uvicorn app:app  (enterprise_app + auth_app + shared)
  freelancer/backend/    → uvicorn app:app  (contributor_app + auth_app + shared)
  reviewer/backend/      → uvicorn app:app  (superadmin_app + auth_app + shared)
```

Each `<role>/backend` contains:
- `app.py` — the role's FastAPI entrypoint (`app:app`)
- the role service package (`mentor_app`, `superadmin_app`, `enterprise_app`, `contributor_app`)
- `auth_app` — shared login/auth endpoints (every role needs sign-in)
- `shared/` — shared config, db, security, mailer helpers
- `requirements.txt`
- `.env` — per-role environment (DB URL, secrets, SMTP). Gitignored.

> Reviewer reuses `superadmin_app` (the reviewer endpoints live there); it runs
> as its own process on a separate port.

## Ports

| Role        | Port |
|-------------|------|
| mentor      | 8101 |
| super-admin | 8102 |
| enterprise  | 8103 |
| freelancer  | 8104 |
| reviewer    | 8105 |

## Run

All at once:

```powershell
powershell -ExecutionPolicy Bypass -File backends\run_all.ps1
```

A single role (example: mentor):

```powershell
cd backends\mentor\backend
$env:PYTHONPATH = (Get-Location).Path
python -m uvicorn app:app --host 127.0.0.1 --port 8101
```

## Notes

- This folder is a copy of the per-role backends under `apps/<role>/backend`.
  The `apps/` copies remain in place; pick one source of truth before diverging.
- `.env` files are gitignored — copy each role's `.env` in before running, or
  set the same environment variables another way.
