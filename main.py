from typing import List, Optional
import json
import logging
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

import cache
from database import Base, engine, get_db
from models import TaskModel


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": self.formatTime(
                record,
                datefmt="%Y-%m-%dT%H:%M:%S",
            ),
            "level": record.levelname,
            "message": record.getMessage(),
        }

        if hasattr(record, "trace_id"):
            log_entry["trace_id"] = record.trace_id

        if hasattr(record, "method"):
            log_entry["method"] = record.method

        if hasattr(record, "path"):
            log_entry["path"] = record.path

        if hasattr(record, "status"):
            log_entry["status"] = record.status

        return json.dumps(log_entry)


logger = logging.getLogger("taskly")
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())

logger.handlers.clear()
logger.addHandler(handler)
logger.propagate = False


# -----------------------------------------------------------------------------
# Application
# -----------------------------------------------------------------------------

app = FastAPI(
    title="Tasks API",
    description="Bootcamp demo app — Week 1/2/8",
)


# -----------------------------------------------------------------------------
# Prometheus metrics
# -----------------------------------------------------------------------------

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)

REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
)

TASKS_CREATED = Counter(
    "tasks_created_total",
    "Total number of tasks created",
)

ACTIVE_TASKS = Gauge(
    "active_tasks",
    "Current number of active tasks",
)


def update_active_tasks(db: Session):
    """Update the active tasks gauge from the database."""
    active_count = (
        db.query(TaskModel)
        .filter(TaskModel.done.is_(False))
        .count()
    )

    ACTIVE_TASKS.set(active_count)


# -----------------------------------------------------------------------------
# Request middleware
# -----------------------------------------------------------------------------

@app.middleware("http")
async def request_middleware(request: Request, call_next):
    start = time.time()

    trace_id = request.headers.get("X-Trace-ID") or str(uuid.uuid4())

    response = await call_next(request)

    duration = time.time() - start
    path = request.url.path
    status = response.status_code

    REQUEST_COUNT.labels(
        method=request.method,
        path=path,
        status=str(status),
    ).inc()

    REQUEST_LATENCY.labels(
        method=request.method,
        path=path,
    ).observe(duration)

    logger.info(
        "request completed",
        extra={
            "trace_id": trace_id,
            "method": request.method,
            "path": path,
            "status": status,
        },
    )

    response.headers["X-Trace-ID"] = trace_id

    return response


# -----------------------------------------------------------------------------
# Prometheus endpoint
# -----------------------------------------------------------------------------

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


Base.metadata.create_all(bind=engine)


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------

class Task(BaseModel):
    title: str
    description: Optional[str] = None
    done: bool = False
    priority: Optional[str] = None  # "low" | "medium" | "high"


class TaskOut(Task):
    id: int

    model_config = {"from_attributes": True}


# -----------------------------------------------------------------------------
# API endpoints
# -----------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "service": "tasks-api"}


@app.get("/tasks", response_model=List[TaskOut])
def list_tasks(db: Session = Depends(get_db)):
    cached = cache.get_cached_tasks()

    if cached is not None:
        update_active_tasks(db)
        return cached

    result = [
        TaskOut.model_validate(t).model_dump()
        for t in db.query(TaskModel).order_by(TaskModel.id).all()
    ]

    cache.set_cached_tasks(result)
    update_active_tasks(db)

    return result


@app.post("/tasks", response_model=TaskOut, status_code=201)
def create_task(task: Task, db: Session = Depends(get_db)):
    db_task = TaskModel(**task.model_dump())

    db.add(db_task)
    db.commit()
    db.refresh(db_task)

    TASKS_CREATED.inc()

    cache.invalidate_task_cache(db_task.id)
    update_active_tasks(db)

    logger.info(
        "task created",
        extra={
            "task_id": db_task.id,
            "task_title": db_task.title,
        },
    )

    return db_task


@app.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)):
    cached = cache.get_cached_task(task_id)

    if cached is not None:
        return cached

    db_task = db.get(TaskModel, task_id)

    if db_task is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    result = TaskOut.model_validate(db_task).model_dump()

    cache.set_cached_task(task_id, result)

    return result


@app.patch("/tasks/{task_id}", response_model=TaskOut)
def update_task(
    task_id: int,
    task: Task,
    db: Session = Depends(get_db),
):
    db_task = db.get(TaskModel, task_id)

    if db_task is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    for field, value in task.model_dump().items():
        setattr(db_task, field, value)

    db.commit()
    db.refresh(db_task)

    cache.invalidate_task_cache(task_id)
    update_active_tasks(db)

    return db_task


@app.delete("/tasks/{task_id}", status_code=204)
def delete_task(
    task_id: int,
    db: Session = Depends(get_db),
):
    db_task = db.get(TaskModel, task_id)

    if db_task is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    db.delete(db_task)
    db.commit()

    cache.invalidate_task_cache(task_id)
    update_active_tasks(db)