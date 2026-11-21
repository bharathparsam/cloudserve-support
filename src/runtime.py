from .config import Settings
from .logging_store import Store
from .classify import Classifier
from .retrieve import Retriever
from .graph import PipelineService
import json
from .operator_settings import read_operator_settings, build_providers


def apply_operator_values(settings, values):
    settings.model = values["generation_model"]
    settings.decider_model = values["decider_model"]
    settings.decider_mode = values["decider_mode"]
    settings.timeout_seconds = values["timeout_seconds"]
    settings.max_retries = values["max_retries"]
    settings.generation_mode = values.get("generation_mode", "free_only")
    settings.generation_cost_policy = (
        "paid_test_only" if settings.generation_mode == "paid_test" else "free_only"
    )
    settings.paid_test_budget_usd = (
        "0.25" if settings.generation_mode == "paid_test" else None
    )
    return settings


def load_configuration(settings, store=None):
    return apply_operator_values(
        settings, read_operator_settings(settings, store or Store(settings.db_path))
    )


def create_runtime(settings=None):
    settings = settings or Settings()
    store = Store(settings.db_path)
    values = read_operator_settings(settings, store)
    apply_operator_values(settings, values)
    classifier = Classifier(settings.classifier_dir)
    retriever = Retriever(settings.corpus_path, settings.chroma_path)
    provider, evaluator = build_providers(settings, values)
    policy = (
        json.loads(settings.policy_path.read_text())
        if settings.policy_path.exists()
        else {}
    )
    pipeline = PipelineService(
        store,
        classifier,
        retriever,
        provider,
        policy,
        evaluator=evaluator,
        decider_mode=settings.decider_mode,
    )
    from evaluation.harness import tree_hash

    pipeline.code_hash = tree_hash(settings.root)
    pipeline.settings = settings
    pipeline.storage_path = settings.storage
    return settings, store, pipeline
