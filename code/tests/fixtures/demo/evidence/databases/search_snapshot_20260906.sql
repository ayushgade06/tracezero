-- Demo point-in-time export of the search document store (synthetic data)
-- Captured at: 2026-09-06T00:00:00Z

BEGIN TRANSACTION;
CREATE TABLE customer_search_documents (
    resource_id TEXT PRIMARY KEY,
    origin_system TEXT NOT NULL,
    collection_name TEXT NOT NULL,
    record_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO "customer_search_documents" VALUES('search_9101','search_service','customer_search_documents','{"resource_id":"search_9101","display_name":"Mira Holt","email":"mira.holt@example.org"}','2026-09-06T00:00:00Z');
COMMIT;
