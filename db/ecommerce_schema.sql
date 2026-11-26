-- 7-table e-commerce schema. Loaded by the postgres container on first boot
-- (docker-compose mounts it as 01_schema.sql). Rows are loaded by db/seed_ecommerce.py.
--
-- Indexing is deliberately "realistic, not perfect": every foreign key and the main
-- time column are indexed; low-cardinality and rarely-filtered columns
-- (order_items.discount_pct, order_items.quantity, customers.city) are not, so the
-- EXPLAIN gate has a genuine unindexed large-table scan to refuse.

CREATE TABLE categories (
    id          integer PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    parent_id   integer REFERENCES categories (id)   -- NULL for top-level categories
);

CREATE TABLE customers (
    id               integer PRIMARY KEY,
    email            text NOT NULL UNIQUE,
    full_name        text NOT NULL,
    country          text NOT NULL,
    city             text NOT NULL,
    segment          text NOT NULL CHECK (segment IN ('consumer', 'corporate', 'small_business')),
    signup_date      date NOT NULL,
    is_active        boolean NOT NULL,
    marketing_opt_in boolean NOT NULL
);

CREATE TABLE products (
    id           integer PRIMARY KEY,
    sku          text NOT NULL UNIQUE,
    name         text NOT NULL,
    category_id  integer NOT NULL REFERENCES categories (id),
    price        numeric(10,2) NOT NULL CHECK (price > 0),
    cost         numeric(10,2) NOT NULL,
    stock_qty    integer NOT NULL,
    is_active    boolean NOT NULL,
    created_at   timestamptz NOT NULL
);

CREATE TABLE orders (
    id               integer PRIMARY KEY,
    customer_id      integer NOT NULL REFERENCES customers (id),
    status           text NOT NULL CHECK (status IN
                       ('placed', 'paid', 'shipped', 'delivered', 'cancelled', 'refunded')),
    order_date       timestamptz NOT NULL,
    shipping_country text NOT NULL,
    total_amount     numeric(12,2) NOT NULL,           -- sum of line totals, after discounts
    coupon_code      text,                             -- NULL when no coupon was used
    shipped_at       timestamptz,                      -- NULL until shipped
    delivered_at     timestamptz                       -- NULL until delivered
);

CREATE TABLE order_items (
    id            integer PRIMARY KEY,
    order_id      integer NOT NULL REFERENCES orders (id),
    product_id    integer NOT NULL REFERENCES products (id),
    quantity      integer NOT NULL CHECK (quantity > 0),
    unit_price    numeric(10,2) NOT NULL,
    discount_pct  numeric(4,2) NOT NULL DEFAULT 0       -- 0.00 .. 0.50
);

CREATE TABLE payments (
    id         integer PRIMARY KEY,
    order_id   integer NOT NULL REFERENCES orders (id),
    method     text NOT NULL CHECK (method IN ('card', 'paypal', 'bank_transfer', 'gift_card')),
    status     text NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed', 'refunded')),
    amount     numeric(12,2) NOT NULL,
    paid_at    timestamptz                              -- NULL unless succeeded/refunded
);

CREATE TABLE reviews (
    id           integer PRIMARY KEY,
    product_id   integer NOT NULL REFERENCES products (id),
    customer_id  integer NOT NULL REFERENCES customers (id),
    rating       integer NOT NULL CHECK (rating BETWEEN 1 AND 5),
    title        text NOT NULL,
    body         text,                                  -- NULL when the reviewer left no text
    created_at   timestamptz NOT NULL,
    UNIQUE (product_id, customer_id)
);

-- Foreign-key and time indexes.
CREATE INDEX idx_products_category    ON products (category_id);
CREATE INDEX idx_orders_customer      ON orders (customer_id);
CREATE INDEX idx_orders_order_date    ON orders (order_date);
CREATE INDEX idx_order_items_order    ON order_items (order_id);
CREATE INDEX idx_order_items_product  ON order_items (product_id);
CREATE INDEX idx_payments_order       ON payments (order_id);
CREATE INDEX idx_reviews_customer     ON reviews (customer_id);

COMMENT ON TABLE  orders IS 'One row per customer order. total_amount is in dollars.';
COMMENT ON COLUMN orders.status IS 'placed -> paid -> shipped -> delivered; or cancelled / refunded.';
COMMENT ON TABLE  order_items IS 'Line items. Line revenue = quantity * unit_price * (1 - discount_pct).';
COMMENT ON TABLE  payments IS 'One payment attempt per order. Money moved only when status = succeeded.';
COMMENT ON COLUMN customers.segment IS 'consumer | corporate | small_business';
