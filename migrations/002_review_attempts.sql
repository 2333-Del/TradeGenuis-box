CREATE TABLE IF NOT EXISTS review_attempts (
 batch_id uuid NOT NULL, code text NOT NULL, job_id uuid REFERENCES review_jobs(id),
 updated_at timestamptz NOT NULL DEFAULT now(), status text NOT NULL, error text,
 PRIMARY KEY(batch_id,code), FOREIGN KEY(batch_id,code) REFERENCES signal_snapshots(batch_id,code)
);
