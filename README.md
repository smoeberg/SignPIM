# Signalement v6.1 - Model-Driven PIM Platform

Signalement (internally branded "SignPIM") is a multi-tenant SaaS Product Information Management (PIM) system. Version 6.1 marks a major architectural shift from a traditional Node.js REST API to a **Metadata-Driven Workflow Engine**.

## Architecture Overview

The system is built on a "Software Factory" model where business logic, entity schemas, and integration workflows are defined as declarative YAML files.

### 1. Meta Layer (`/meta`)
The "Source of Truth" for the entire platform.
- **Entities**: Data schemas for products, tenants, etc.
- **Rules**: Declarative business rules (EAN validation, quality scores, etc.).
- **Workflows**: Step-by-step processing pipelines (Import, Sync, Publish).

### 2. Engine Layer (`/engine`)
A generic, domain-agnostic runtime that interprets the Meta Layer.
- **Runtime**: Loads metadata and executes workflows.
- **Rule Evaluator**: Safe, non-eval based logic dispatcher for business rules.

### 3. Core Layer (`/core`)
Essential infrastructure services.
- **Database**: Parameterized, SQL-injection-safe persistence with tenant isolation.

### 4. Legacy Layer (`/legacy`)
The original Node.js v3.2 codebase, preserved for reference and phased migration.

## Getting Started

### Prerequisites
- Python 3.8+
- PostgreSQL

### Installation
```bash
pip install -r requirements.txt
```

### Running the Engine
The main entry point demonstrates a full sync workflow:
```bash
export DATABASE_URL="postgresql://user:pass@localhost:5432/pim_db"
python3 main.py
```

## Security & Compliance
- **Tenant Isolation**: Strictly enforced at the Engine and Database layers.
- **SQL Injection Protection**: All queries use parameterized values.
- **No Dangerous Execution**: YAML rules are parsed through a secure dispatcher, avoiding `eval()`.

## Development
- Add new rules in `meta/rules/`
- Add new workflows in `meta/workflow/`
- Define new entities in `meta/entities/`
