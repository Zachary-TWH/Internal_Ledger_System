import os
from app import models
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.main import app
from app.database import Base
from app.main import get_db
import threading
from sqlalchemy import text
from alembic import command
from alembic.config import Config
from app import models
from decimal import Decimal

app.state.limiter.enabled = False  

TEST_DATABASE_URL = "postgresql://postgres:mysecret@localhost:5432/ledger_test"

engine = create_engine(TEST_DATABASE_URL)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)

@pytest.fixture(scope="session", autouse=True)
def _client_lifespan():
    with client:
        yield

def run_migrations_up():
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL 
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")

def run_migrations_down():
    pass  # schema gets dropped at the start of the next run instead

@pytest.fixture(autouse=True)
def setup_database():
    run_migrations_up()
    yield

def get_auth_headers():
    client.post("/register", json={"username": "testuser", "password": "testpass123"})
    response = client.post("/login", data={"username": "testuser", "password": "testpass123"})
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}

def test_create_account():
    headers = get_auth_headers()
    response = client.post("/accounts", json={"owner_name": "Alice", "currency": "USD"}, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["owner_name"] == "Alice"
    assert data["balance"] == 0.0

def test_deposit():
    headers = get_auth_headers()
    account = client.post("/accounts", json={"owner_name": "Alice", "currency": "USD"}, headers=headers).json()
    client.post("/accounts", json={"owner_name": "EXTERNAL", "currency": "USD"}, headers=headers)

    response = client.post("/deposit", json={
        "account_id": account["id"],
        "amount": 100,
        "idempotency_key": "dep-001"
    }, headers=headers)
    assert response.status_code == 200
    assert response.json()["new_balance"] == 100.0

def test_withdraw_insufficient_funds():
    headers = get_auth_headers()
    account = client.post("/accounts", json={"owner_name": "Alice", "currency": "USD"}, headers=headers).json()
    client.post("/accounts", json={"owner_name": "EXTERNAL", "currency": "USD"}, headers=headers)

    response = client.post("/withdraw", json={
        "account_id": account["id"],
        "amount": 100,
        "idempotency_key": "wdr-001"
    }, headers=headers)
    assert response.status_code == 400
    assert response.json()["detail"] == "Insufficient funds"

def test_transfer():
    headers = get_auth_headers()
    alice = client.post("/accounts", json={"owner_name": "Alice", "currency": "USD"}, headers=headers).json()
    bob = client.post("/accounts", json={"owner_name": "Bob", "currency": "USD"}, headers=headers).json()
    client.post("/accounts", json={"owner_name": "EXTERNAL", "currency": "USD"}, headers=headers)

    client.post("/deposit", json={
        "account_id": alice["id"],
        "amount": 100,
        "idempotency_key": "dep-001"
    }, headers=headers)

    response = client.post("/transfer", json={
        "from_account_id": alice["id"],
        "to_account_id": bob["id"],
        "amount": 40,
        "idempotency_key": "txn-001"
    }, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["from_account_new_balance"] == 60.0
    assert data["to_account_new_balance"] == 40.0

def test_transfer_same_account():
    headers = get_auth_headers()
    alice = client.post("/accounts", json={"owner_name": "Alice", "currency": "USD"}, headers=headers).json()
    response = client.post("/transfer", json={
        "from_account_id": alice["id"],
        "to_account_id": alice["id"],
        "amount": 10,
        "idempotency_key": "txn-002"
    }, headers=headers)
    assert response.status_code == 400
    assert response.json()["detail"] == "Cannot transfer to the same account"

def test_concurrent_deposits():
    headers = get_auth_headers()
    alice = client.post("/accounts", json={"owner_name": "Alice", "currency": "USD"}, headers=headers).json()
    client.post("/accounts", json={"owner_name": "EXTERNAL", "currency": "USD"}, headers=headers)

    def deposit():
        client.post("/deposit", json={
            "account_id": alice["id"],
            "amount": 50,
            "idempotency_key": f"dep-{threading.get_ident()}"
        }, headers=headers)

    t1 = threading.Thread(target=deposit)
    t2 = threading.Thread(target=deposit)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    response = client.get(f"/accounts/{alice['id']}", headers=headers)
    assert response.json()["balance"] == 100.0


# --- /refresh and /logout tests ---

def login_and_get_tokens():
    client.post("/register", json={"username": "refreshuser", "password": "testpass123"})
    response = client.post("/login", data={"username": "refreshuser", "password": "testpass123"})
    data = response.json()
    return data["access_token"], data["refresh_token"]

def test_refresh_returns_new_access_token():
    access_token, refresh_token = login_and_get_tokens()

    response = client.post("/refresh", json={"refresh_token": refresh_token})
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert len(data["access_token"]) > 0

def test_refresh_with_invalid_token_fails():
    response = client.post("/refresh", json={"refresh_token": "this-token-does-not-exist"})
    assert response.status_code == 401

def test_logout_then_refresh_fails():
    access_token, refresh_token = login_and_get_tokens()
    headers = {"Authorization": f"Bearer {access_token}"}

    logout_response = client.post("/logout", json={"refresh_token": refresh_token}, headers=headers)
    assert logout_response.status_code == 200

    # the same refresh token should no longer work after logout
    refresh_response = client.post("/refresh", json={"refresh_token": refresh_token})
    assert refresh_response.status_code == 401


# --- currency mismatch / FX conversion tests ---

def test_transfer_cross_currency_without_rate_fails():
    headers = get_auth_headers()
    usd_account = client.post("/accounts", json={"owner_name": "Alice", "currency": "USD"}, headers=headers).json()
    xyz_account = client.post("/accounts", json={"owner_name": "Bob", "currency": "XYZ"}, headers=headers).json()
    client.post("/accounts", json={"owner_name": "EXTERNAL", "currency": "USD"}, headers=headers)

    client.post("/deposit", json={
        "account_id": usd_account["id"],
        "amount": 100,
        "idempotency_key": "fx-test-dep-1"
    }, headers=headers)

    # no ExchangeRate row exists for USD -> XYZ, since XYZ isn't a real/seeded currency
    response = client.post("/transfer", json={
        "from_account_id": usd_account["id"],
        "to_account_id": xyz_account["id"],
        "amount": 50,
        "idempotency_key": "fx-test-txn-1"
    }, headers=headers)

    assert response.status_code == 400
    assert "exchange rate" in response.json()["detail"].lower()

def test_transfer_cross_currency_converts_with_seeded_rate():
    headers = get_auth_headers()
    usd_account = client.post("/accounts", json={"owner_name": "Alice", "currency": "USD"}, headers=headers).json()
    eur_account = client.post("/accounts", json={"owner_name": "Bob", "currency": "EUR"}, headers=headers).json()
    client.post("/accounts", json={"owner_name": "EXTERNAL", "currency": "USD"}, headers=headers)

    client.post("/deposit", json={
        "account_id": usd_account["id"],
        "amount": 100,
        "idempotency_key": "fx-test-dep-2"
    }, headers=headers)

    # manually seed a known rate, so the test doesn't depend on the real background job or live API
    db = TestingSessionLocal()
    db.add(models.ExchangeRate(base_currency="USD", quote_currency="EUR", rate=Decimal("0.9")))
    db.commit()
    db.close()

    response = client.post("/transfer", json={
        "from_account_id": usd_account["id"],
        "to_account_id": eur_account["id"],
        "amount": 100,
        "idempotency_key": "fx-test-txn-2"
    }, headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["exchange_rate_used"] == 0.9
    assert data["from_account_new_balance"] == 0.0
    assert data["to_account_new_balance"] == 90.0

def test_transfer_same_currency_has_no_exchange_rate():
    headers = get_auth_headers()
    usd_account_1 = client.post("/accounts", json={"owner_name": "Alice", "currency": "USD"}, headers=headers).json()
    usd_account_2 = client.post("/accounts", json={"owner_name": "Bob", "currency": "USD"}, headers=headers).json()
    client.post("/accounts", json={"owner_name": "EXTERNAL", "currency": "USD"}, headers=headers)

    client.post("/deposit", json={
        "account_id": usd_account_1["id"],
        "amount": 100,
        "idempotency_key": "fx-test-dep-3"
    }, headers=headers)

    response = client.post("/transfer", json={
        "from_account_id": usd_account_1["id"],
        "to_account_id": usd_account_2["id"],
        "amount": 100,
        "idempotency_key": "fx-test-txn-3"
    }, headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["exchange_rate_used"] is None
    assert data["to_account_new_balance"] == 100.0