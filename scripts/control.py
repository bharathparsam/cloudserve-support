"""Inspect, stop or explicitly enable local automatic answers."""

import argparse
import json
from filelock import FileLock
from src.config import Settings
from src.logging_store import Store
from src.runtime import create_runtime


def enable(runtime, reason):
    settings, store, pipeline = runtime
    with FileLock(str(settings.storage / "pipeline.lock")).acquire(timeout=0):
        if not (
            pipeline.classifier.ready
            and pipeline.retriever.ready
            and pipeline.policy.get("calibration_ready")
            and pipeline.policy.get("retrieval_ready")
        ):
            raise RuntimeError(
                "Classifier, retrieval and measured policy must be ready"
            )
        if not pipeline.provider.preflight().get("verified"):
            raise RuntimeError("Provider could not be verified")
        return store.set_control(True, reason, "local_cli")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--stop", action="store_true")
    actions.add_argument("--enable", action="store_true")
    parser.add_argument("--reason", default="Local operator command")
    args = parser.parse_args()
    settings = Settings()
    store = Store(settings.db_path)
    try:
        if args.stop:
            # Intentionally does not acquire the pipeline lock: stops must take
            # effect while a batch or model request is in flight.
            result = store.set_control(False, args.reason, "local_cli")
        elif args.enable:
            result = enable(create_runtime(settings, isolated_index=True), args.reason)
        else:
            result = store.control()
    except Exception as error:
        raise SystemExit(
            f"Control not changed ({type(error).__name__}). Check configuration, assets and provider availability."
        ) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
