 # Internal Ledger System – Take-Home Assignment

---

## Overview

Build a simple financial ledger system that records account transactions and maintains accurate balances. 

---

## Functional Requirements

1. **Record Transactions**
   - POST `/transactions` with: source account, destination account, amount
   - Transaction is recorded atomically (both accounts updated together, or neither)
   - Returns transaction ID and confirmation

2. **Query Balance**
   - GET `/accounts/{id}` returns current balance
   - GET `/accounts/{id}/transactions` returns transaction history (ordered by timestamp)

3. **Transfer Money**
   - Money moves from source to destination in a single atomic operation
   - Source balance decreases, destination increases by same amount
   - If source has insufficient balance, reject the transfer

---

## System Behaviour

Implement as many of the behaviours below as you can within the time available. **For any behaviour you do not implement, explain in your README how you would solve it** — for example, the approach, data model or schema changes, key trade-offs, and how you would test it. A well-reasoned design is valued over a rushed or incomplete implementation.

- Transactions can be reversed (posted as a reversal/correction transaction)

- Repeated submission of the same request should not result in unintended additional transfers

- Verifies that total money in the ledger matches the expected sum and reports any discrepancies

- Handles transactions between accounts in different currencies using exchange rates

- Safely processes multiple simultaneous transfers to the same account without losing updates or creating inconsistencies

- At month-end, accounts are reconciled against transaction history, verifying balances and detecting discrepancies

---

## Notes for Candidates

- **Java** is the preferred implementation language

- **AI is encouraged** for boilerplate, API setup, and exploration

- Focus on correctness and clarity, not fancy UI

- Document assumptions about the ledger's behaviour

- You are welcome to introduce additional design decisions or improvements where you can justify their value

---

## Submission

Please share your GitHub repository link when complete.
