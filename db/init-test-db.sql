-- Docker Compose mounts this file into Postgres's initialization directory.
-- Postgres runs it only when creating a fresh data volume, alongside the main
-- "ledger" database. This separate empty database is used by
-- tests/test_postgres_concurrency.py via TEST_POSTGRES_URL; that test suite
-- drops and recreates its tables, so do not point it at the main ledger database.
CREATE DATABASE ledger_test;