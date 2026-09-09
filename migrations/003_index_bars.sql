CREATE TABLE IF NOT EXISTS index_bars (
 day date PRIMARY KEY, close numeric NOT NULL, source text NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_code ON review_observations(code, fetched_at DESC);
