from celery import Celery
from celery.schedules import crontab
from dotenv import load_dotenv
import os

load_dotenv()

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672//")

celery_app = Celery("ledger", broker=RABBITMQ_URL, include=["app.tasks"])

celery_app.conf.beat_schedule = {
    "reconcile-every-minute": {
        "task": "app.tasks.reconcile_all_accounts_task",
        "schedule": 60.0,  # seconds
    },
    "fetch-fx-rates-hourly": {
        "task": "app.tasks.fetch_exchange_rates_task",
        "schedule": crontab(minute=0),  # top of every hour
    },
}