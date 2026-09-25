# Parle-G Agentic AI Supply Chain Engine

## Project Overview
An agentic AI system that automates order prioritization and warehouse routing for Parle-G's retail orders. The engine processes incoming retail demand, evaluates fulfillment priority based on customer type and order urgency, and allocates inventory across regional fulfillment nodes.

## Agents Built So Far
1. **Order Intake Agent (Done)** - Assigns priority scores based on customer type (Government > NGO > Loyal > Regular) and evaluates urgency for Regular orders.
2. **Spatial Routing Agent (Done)** - Matches orders to their region's warehouse and checks stock availability.
3. **Rebalancing Agent (Planned)** - Handles inter-warehouse stock transfers.
4. **Guardrail Agent (Planned)** - Human approval for large transfers.

## Tech Stack
- **Python**
- **Google Agent Development Kit (ADK)**
- **Gemini Enterprise Agent Platform**
- **Firestore**
- **BigQuery**
- **Streamlit + Cloud Run**
- **Google Antigravity (dev environment)**

> **Note:** Currently using local mock JSON data (`data/orders.json`, `data/warehouses.json`) for development. Firestore integration is pending GCP billing verification.

## How to Run Tests
Ensure dependencies are installed and run the test suites with `pytest`:

- **Run all tests:**
  ```bash
  pytest
  ```

- **Order Intake Agent tests:**
  ```bash
  pytest tests/test_order_intake_agent.py
  ```

- **Spatial Routing Agent tests:**
  ```bash
  pytest tests/test_spatial_routing_agent.py
  ```
