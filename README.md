# Employee Attendance & Analytics API

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export MONGO_DB="attendance_db"
python sample_seed.py
uvicorn app.main:app --port 8000
```

On Windows PowerShell, set environment variables with:

```powershell
$env:MONGO_URI="mongodb://localhost:27017"
$env:MONGO_DB="attendance_db"
```

The app creates its MongoDB indexes at startup. `GET /health` returns 200 only after MongoDB responds to `pin

All application code is in `app/main.py`. Instants are accepted and returned as epoch milliseconds, while MongoDB stores them as UTC BSON datetimes. Attendance dates, months and shift times remain strings as required by the contract.
