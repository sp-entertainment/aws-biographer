-- 002_resource_embeddings.sql
--
-- Vectors do not represent edges -- similarity is not connection, and two
-- Lambdas doing similar work sit close together with no relationship at all.
-- Their job here is narrower and genuinely useful: PROPOSING candidate edges.
-- Resources that cluster tightly while sharing no known edge become questions
-- the agent asks, and the user's answer writes a confirmed edge.
--
-- Nullable on purpose. A resource without an embedding is simply not a
-- clustering candidate; it is not an error, and backfilling is incremental.

ALTER TABLE resources ADD COLUMN IF NOT EXISTS embedding VECTOR(1024);
ALTER TABLE resources ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ;
