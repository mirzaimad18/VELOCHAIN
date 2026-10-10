# VELOCHAIN - Autonomous Agentic Supply Chain for High-Volume FMCG

**An agentic supply-chain management system for prioritizing and fulfilling high-volume FMCG orders, starting with Parle-G.**

VELOCHAIN turns incoming store orders into a coordinated workflow: it extracts order details, calculates priority, checks warehouse stock and distance, proposes stock transfers when needed, and routes large transfers through manager approval.

## The problem

High-volume FMCG networks need to move large quantities quickly across stores, urban hubs, and rural depots. Traditional workflows often rely on disconnected emails, manual prioritization, and delayed inventory decisions. As a result:

- Orders can be processed without consistently considering urgency, quantity, or store loyalty.
- Urban stores can face stockouts even while inventory is available elsewhere.
- Rural depots can accumulate slow-moving stock while nearby urban demand goes unmet.
- Managers may lack a clear, auditable approval point for large stock movements.

VELOCHAIN connects order intake, spatial routing, inventory rebalancing, and supervisory controls in one LangGraph workflow.

## How it works

The application accepts a store order email in its Streamlit dashboard. The agents process it in sequence, and the dashboard displays workflow events, priority, assigned warehouse, and approval status.

### The four agents

1. **Order Intake Agent**
   - Extracts a structured order from email text using Gemini 2.5 Flash and Pydantic structured output.
   - Falls back to regular-expression extraction when no Gemini API key is configured or the Gemini request encounters a supported API or network error.
   - Resolves the store's loyalty tier from the database and calculates a priority score from urgency, quantity, and loyalty:

     ```text
     priority = urgency weight + loyalty weight + min(quantity / 1,000, 20)
     ```

     Urgency weights are High = 50, Medium = 30, and Low = 10. Loyalty weights are Gold = 30, Silver = 20, and Bronze = 10.

2. **Spatial Routing Agent**
   - Calculates great-circle distance with the Haversine formula.
   - Assigns an order to the nearest warehouse with enough Parle-G 100g inventory.
   - Requests inventory rebalancing when no single warehouse can fulfill the order.

3. **Inventory Rebalancing Agent**
   - Finds a destination warehouse with a stock shortfall and selects a source with excess inventory.
   - Prioritizes rural depots as transfer sources, then considers available stock and distance.
   - Proposes the quantity and source/destination for the supervisor to validate.

4. **Supervisor Agent**
   - Validates the workflow state and transfer proposal against typed schemas before acting.
   - Persists a pending transfer proposal in the configured database.
   - Pauses the workflow for Human-in-the-Loop manager review when a transfer exceeds **50,000 units**. The manager can approve or reject it in the dashboard.

## Key features

- Email-to-order processing with structured Gemini output and a regex fallback.
- Dynamic order prioritization using urgency, order quantity, and loyalty tier.
- Nearest-in-stock warehouse routing based on geographic coordinates.
- Inter-warehouse transfer proposals to address inventory imbalances.
- Human approval for transfers above 50,000 units.
- Live workflow status and order metrics in a Streamlit dashboard.
- SQLite by default, with PostgreSQL support through `DATABASE_URL`.
- Pytest coverage for core order, routing, and workflow behavior.

## Technology stack

- **Python 3.10+**
- **LangGraph** and **LangChain** for agent orchestration
- **Google Gemini API** (`gemini-2.5-flash`) through `langchain-google-genai`
- **Pydantic** for data and workflow validation
- **Streamlit** for the interactive dashboard
- **SQLite** by default, or **PostgreSQL** via `psycopg2`
- **Pytest** for tests
- **Docker** for containerized deployment
- **Pandas** for dashboard data handling

## 📧 Sample Demo Emails

Copy and paste one of these messages into the Streamlit dashboard's **Paste Order Email Here** field, then select **Parse & Process Email**. They exercise order intake, warehouse routing, and—where the transfer is over the threshold—the manager approval workflow.

### High Urgency — stock rebalancing and manager approval

```text
Subject: URGENT - Immediate Parle-G Restock for Store 1

Hello Supply Chain Team,

Store 1 urgently needs 60,000 units of Parle-G. Please dispatch this order
immediately and let us know when the stock transfer is ready for approval.

Thank you,
Store Manager
```

This high-priority order exceeds the 50,000-unit transfer approval threshold. With the sample inventory, it should require rebalancing from a warehouse with available stock and pause for manager approval.

### Medium Urgency — standard weekly replenishment

```text
Subject: Weekly Parle-G Replenishment for Store 2

Hello Supply Chain Team,

Store 2 requests 15,000 units of Parle-G for our standard weekly
replenishment. Please process this order through the usual fulfillment flow.

Thank you,
Store Manager
```

### Low Urgency — advance bulk request

```text
Subject: Advance Parle-G Request for Store 3

Hello Supply Chain Team,

Store 3 is placing a low-urgency advance request for 20,000 units of
Parle-G for next month's sale. This order is for planned future demand and
does not require immediate dispatch.

Thank you,
Store Manager
```

## Local setup

### Prerequisites

- Python 3.10 or newer
- Git
- A Gemini API key for Gemini-powered email extraction (optional; regex extraction is used as a fallback)

### 1. Clone the repository

```bash
git clone https://github.com/mirzaimad18/VELOCHAIN.git
cd VELOCHAIN
```

### 2. Create and activate a virtual environment

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file in the repository root:

```dotenv
# Optional for email parsing; without it the app uses regex extraction.
GEMINI_API_KEY=your_gemini_api_key

# Optional; defaults to a local SQLite database at ./supply_chain.db.
# For PostgreSQL, set DATABASE_URL to your PostgreSQL connection URL.
```

Keep real credentials private; do not commit `.env`.

### 5. Initialize and seed the database

```bash
python -m database.seed
```

This initializes the schema and loads sample stores, warehouses, and Parle-G 100g inventory. The seed command can be run again to refresh the sample records.

### 6. Run the tests

```bash
pytest
```

### 7. Launch the dashboard

```bash
streamlit run app.py
```

Open the local URL printed by Streamlit, paste a store order email into **Parse Store Order Email**, and select **Parse & Process Email**. Use **Load sample data** in the dashboard if you want to restore the sample records.

## Docker

Build and start the Streamlit application from the repository root:

```bash
docker build -t velochain .
docker run --rm -p 8501:8501 --env-file .env velochain
```

The dashboard will be available at `http://localhost:8501`. The container uses the same `DATABASE_URL` configuration; if you use PostgreSQL, provide a database URL reachable from the container.

## Repository layout

```text
.
├── agents/                     # Email parsing and order ranking
├── database/                   # Database connection, schema, and seed data
├── tests/                      # Pytest tests
├── tools/                      # Spatial routing and distance calculations
├── app.py                      # Streamlit dashboard
├── workflow.py                 # LangGraph workflow
├── order_intake.py             # Priority scoring and workflow intake
├── spatial_routing_agent.py    # Warehouse selection
├── inventory_rebalancing_agent.py
├── supervisor_agent.py         # Validation and approval handling
├── requirements.txt
└── Dockerfile
```

## Database configuration

VELOCHAIN reads `DATABASE_URL` from the environment. If it is unset, the application uses:

```text
sqlite:///./supply_chain.db
```

The database schema supports SQLite and PostgreSQL. Initialize and seed the selected database with `python -m database.seed`.
