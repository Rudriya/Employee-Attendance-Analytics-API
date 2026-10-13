# Employee Attendance API

A FastAPI service backed by MongoDB 6.0 or newer. Python 3.11+ is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000
```

The app creates its indexes when it starts. Use `GET /health` to check the MongoDB connection. For a quick local check, create an employee with `POST /employees`, then try the punch-in and analytics routes. The assignment's seed data is not included in this repository.
