-- Portable schema for PostgreSQL and SQLite.
-- IDs are application-assigned so the same definitions work in both databases.

CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    loyalty_tier TEXT NOT NULL
        CHECK (loyalty_tier IN ('Gold', 'Silver', 'Bronze'))
);

CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    type TEXT NOT NULL
        CHECK (type IN ('Urban Hub', 'Rural Depot'))
);

CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY,
    warehouse_id INTEGER NOT NULL,
    product_name TEXT NOT NULL DEFAULT 'Parle-G 100g',
    stock_quantity INTEGER NOT NULL CHECK (stock_quantity >= 0),
    last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (warehouse_id) REFERENCES warehouses (id)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    urgency_level TEXT NOT NULL
        CHECK (urgency_level IN ('High', 'Medium', 'Low')),
    priority_score REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pending'
        CHECK (status IN ('Pending', 'Dispatched', 'Requires Approval')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (store_id) REFERENCES stores (id)
);

CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY,
    from_warehouse_id INTEGER NOT NULL,
    to_warehouse_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    status TEXT NOT NULL DEFAULT 'Pending Approval'
        CHECK (status IN ('Pending Approval', 'Approved', 'Completed', 'Rejected')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (from_warehouse_id) REFERENCES warehouses (id),
    FOREIGN KEY (to_warehouse_id) REFERENCES warehouses (id),
    CHECK (from_warehouse_id <> to_warehouse_id)
);

CREATE INDEX IF NOT EXISTS idx_stores_city
    ON stores (city);
CREATE INDEX IF NOT EXISTS idx_warehouses_city
    ON warehouses (city);
CREATE INDEX IF NOT EXISTS idx_inventory_warehouse_id
    ON inventory (warehouse_id);
CREATE INDEX IF NOT EXISTS idx_orders_store_id
    ON orders (store_id);
CREATE INDEX IF NOT EXISTS idx_orders_status
    ON orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at
    ON orders (created_at);
CREATE INDEX IF NOT EXISTS idx_transfers_from_warehouse_id
    ON transfers (from_warehouse_id);
CREATE INDEX IF NOT EXISTS idx_transfers_to_warehouse_id
    ON transfers (to_warehouse_id);
CREATE INDEX IF NOT EXISTS idx_transfers_status
    ON transfers (status);
