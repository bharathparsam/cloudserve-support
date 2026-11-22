import threading
from .ingest import now
from filelock import FileLock, Timeout

API_RUN_KINDS = frozenset(("manual", "development_upload", "development_builtin"))


class JobRunner:
    def __init__(self, store, pipeline):
        self.store, self.pipeline = store, pipeline
        self.stopping = threading.Event()
        self.thread = None
        self.last_error = None

    def start(self):
        self.started_at = now()
        self.recovered = False
        self.thread = threading.Thread(
            target=self.run, name="durable-ticket-worker", daemon=True
        )
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread:
            try:
                self.store.set_control(
                    False, "Application shutdown; explicit re-enable required", "system"
                )
            except Exception:
                pass
            self.thread.join(timeout=2)

    def run(self):
        lock = FileLock(str(self.pipeline.storage_path / "pipeline.lock"))
        while not self.stopping.is_set():
            job = None
            try:
                with lock.acquire(timeout=0):
                    if not self.recovered:
                        self.store.recover_interrupted(before=self.started_at)
                        self.recovered = True
                    job = self.store.claim_job(allowed_kinds=API_RUN_KINDS)
                    if job:
                        self.pipeline.process(
                            job["original"],
                            job["run_id"],
                            job["record_id"],
                            job["input_index"],
                        )
                        self.last_error = None
                        if self.store.get_run(job["run_id"])["state"] == "completed":
                            from evaluation.export import export_run

                            export_run(
                                self.pipeline.settings,
                                self.store,
                                self.pipeline,
                                job["run_id"],
                            )
                if not job:
                    self.stopping.wait(0.3)
            except Timeout:
                self.stopping.wait(0.3)
            except Exception as e:
                self.last_error = type(e).__name__
                if job:
                    try:
                        self.store.fail_run(
                            job["run_id"],
                            "Audit or processing failure: " + type(e).__name__,
                        )
                    except Exception:
                        pass
                self.stopping.wait(1)
