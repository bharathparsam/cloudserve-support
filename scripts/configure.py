"""Save local free-model configuration without inference or changing automation."""

import argparse
import json
from filelock import FileLock
from src.config import Settings
from src.logging_store import Store
from src.operator_settings import OperatorSettings, persist_operator_settings


def configure(settings, model, timeout=30.0, retries=2):
    values = OperatorSettings(
        generation_model=model,
        timeout_seconds=float(timeout),
        max_retries=retries,
    )
    store = Store(settings.db_path)
    with FileLock(str(settings.storage / "pipeline.lock")).acquire(timeout=0):
        return persist_operator_settings(store, values)


def main():
    settings = Settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=settings.model)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()
    try:
        result = configure(settings, args.model, args.timeout, args.retries)
    except Exception as error:
        raise SystemExit(
            f"Configuration not saved ({type(error).__name__}). Stop automation and wait for active jobs; use an explicit provider/model:free identifier."
        ) from None
    print(
        json.dumps(
            {
                "model": result["generation_model"],
                "mode": "free_only",
                "revision": result["revision"],
                "provider_verified": False,
            }
        )
    )


if __name__ == "__main__":
    main()
