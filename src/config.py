from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import os
import logging
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    root: Path = ROOT
    storage: Path = field(
        default_factory=lambda: Path(os.getenv("STORAGE_PATH", str(ROOT / "storage")))
    )
    operator_token: str = field(
        default_factory=lambda: os.getenv("OPERATOR_TOKEN", ""), repr=False
    )
    api_key: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""), repr=False
    )
    model: str = field(
        default_factory=lambda: os.getenv("MODEL_NAME")
        or os.getenv("OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it:free")
    )
    embedding_model: str = field(
        default_factory=lambda: os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    )
    retrieval_top_k: int = field(
        default_factory=lambda: int(os.getenv("RETRIEVAL_TOP_K", "4"))
    )
    confidence_threshold: float | None = field(
        default_factory=lambda: (
            float(os.environ["CONFIDENCE_THRESHOLD"])
            if os.getenv("CONFIDENCE_THRESHOLD")
            else None
        )
    )
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper()
    )
    decider_model: str = field(
        default_factory=lambda: os.getenv(
            "DECIDER_MODEL", "perplexity/pplx-decider-flash-v1.3:free"
        )
    )
    # Legacy metadata only; the optional observer is no longer part of runtime.
    decider_mode: str = "disabled"
    mode: str = field(default_factory=lambda: os.getenv("APP_MODE", "live"))

    def __post_init__(self):
        if self.retrieval_top_k < 1:
            raise ValueError("RETRIEVAL_TOP_K must be positive")
        if (
            self.confidence_threshold is not None
            and not 0 <= self.confidence_threshold <= 1
        ):
            raise ValueError("CONFIDENCE_THRESHOLD must be between 0 and 1")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid LOG_LEVEL")
        logging.basicConfig(level=self.log_level)
        logging.getLogger().setLevel(self.log_level)
        if os.getenv("FREE_ONLY", "true").lower() != "true":
            raise ValueError("FREE_ONLY must remain true")
        if self.decider_mode != "disabled":
            raise ValueError(
                "The optional observer has been removed; use disabled mode"
            )
        if self.mode != "live":
            raise ValueError("Runtime only supports live mode; fixtures are test-only")
        self.storage.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_HOME", str(self.storage / "cache/huggingface"))

    @property
    def db_path(self):
        default_storage = Path(os.getenv("STORAGE_PATH", str(ROOT / "storage")))
        if self.storage.resolve() != default_storage.resolve():
            return self.storage / "decisions.db"
        return Path(
            os.getenv("DATABASE_URL", "").removeprefix("sqlite:///")
            or self.storage / "decisions.db"
        )

    @property
    def corpus_path(self):
        return Path(
            os.getenv(
                "CORPUS_PATH", str(self.root / "assets/corpus/documentation.json")
            )
        )

    @property
    def classifier_dir(self):
        return Path(os.getenv("CLASSIFIER_DIR", str(self.root / "assets/models")))

    @property
    def chroma_path(self):
        return Path(
            getattr(self, "chroma_override", None)
            or os.getenv("CHROMA_PATH", str(self.storage / "chroma"))
        )

    @property
    def policy_path(self):
        return Path(os.getenv("POLICY_PATH", str(self.root / "policies/routing.json")))
