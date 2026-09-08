CREATE TABLE IF NOT EXISTS scan_batches (
 id uuid PRIMARY KEY, started_at timestamptz NOT NULL, finished_at timestamptz NOT NULL,
 as_of timestamptz NOT NULL, trade_date date NOT NULL, mode text NOT NULL,
 version text NOT NULL, status text NOT NULL, legacy boolean NOT NULL DEFAULT false,
 meta jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS batches_date ON scan_batches(trade_date DESC, mode, version);
CREATE TABLE IF NOT EXISTS signal_snapshots (
 batch_id uuid REFERENCES scan_batches(id), code text NOT NULL, asset text NOT NULL,
 category text NOT NULL, level text NOT NULL, snapshot jsonb NOT NULL,
 PRIMARY KEY(batch_id, code)
);
CREATE INDEX IF NOT EXISTS signals_code ON signal_snapshots(code, asset);
CREATE TABLE IF NOT EXISTS review_jobs (
 id uuid PRIMARY KEY, status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(), payload jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS review_results (
 batch_id uuid NOT NULL, code text NOT NULL, job_id uuid REFERENCES review_jobs(id),
 updated_at timestamptz NOT NULL DEFAULT now(), result jsonb NOT NULL,
 PRIMARY KEY(batch_id,code), FOREIGN KEY(batch_id,code) REFERENCES signal_snapshots(batch_id,code)
);
CREATE TABLE IF NOT EXISTS review_observations (
 job_id uuid REFERENCES review_jobs(id), code text NOT NULL, fetched_at timestamptz NOT NULL,
 frames jsonb NOT NULL, PRIMARY KEY(job_id,code)
);
CREATE TABLE IF NOT EXISTS review_versions (
 job_id uuid REFERENCES review_jobs(id), batch_id uuid NOT NULL, code text NOT NULL,
 result jsonb NOT NULL, PRIMARY KEY(job_id,batch_id,code)
);
CREATE TABLE IF NOT EXISTS trading_calendar (
 day date PRIMARY KEY, is_open boolean NOT NULL, source text NOT NULL
);
