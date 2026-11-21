from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import os
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
        default_factory=lambda: os.getenv(
            "OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it:free"
        )
    )
    decider_model: str = field(
        default_factory=lambda: os.getenv(
            "DECIDER_MODEL", "perplexity/pplx-decider-flash-v1.3:free"
        )
    )
    decider_mode: str = field(
        default_factory=lambda: os.getenv("DECIDER_MODE", "shadow")
    )
    mode: str = field(default_factory=lambda: os.getenv("APP_MODE", "live"))

    def __post_init__(self):
        if os.getenv("FREE_ONLY", "true").lower() != "true":
            raise ValueError("FREE_ONLY must remain true")
        if self.decider_mode not in ("shadow", "enforce", "disabled"):
            raise ValueError("DECIDER_MODE must be shadow, enforce or disabled")
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
        return Path(os.getenv("CHROMA_PATH", str(self.storage / "chroma")))

    @property
    def policy_path(self):
        return Path(os.getenv("POLICY_PATH", str(self.root / "policies/routing.json")))

    @property
    def decider_policy_path(self):
        return Path(
            os.getenv("DECIDER_POLICY_PATH", str(self.root / "policies/decider.json"))
        )
