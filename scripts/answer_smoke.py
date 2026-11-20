"""One live development-calibration record through the complete isolated workflow.

No fixture providers, response replay, threshold changes, supplied validation or
external customer delivery. Each output must be a fresh empty directory.
"""
from __future__ import annotations
import argparse,hashlib,json,os
from pathlib import Path
from src.config import Settings,ROOT
from src.ingest import normalize
from src.runtime import create_runtime
from evaluation.harness import run


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--development',required=True)
    parser.add_argument('--index',type=int,required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--model',required=True)
    parser.add_argument('--decider-mode',choices=['shadow','disabled'],default='shadow')
    args=parser.parse_args()
    if not args.live:raise SystemExit('Explicit --live required; no fixtures or paid fallback')
    split=json.loads((ROOT/'assets/models/split-manifest.json').read_text())
    if args.index not in split['calibration']:raise SystemExit('Smoke index must belong to development calibration')
    raw=Path(args.development).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=split['data_hash']:raise SystemExit('Input must match trusted development fingerprint')
    record=json.loads(raw)[args.index]
    normalized=normalize(record,'isolated-development-smoke','isolated-development-smoke')
    if not normalized.valid:raise SystemExit('Selected development record is invalid')
    output=Path(args.output).resolve()
    if output.exists():raise SystemExit('Preserve prior evidence; select a new output directory')
    output.mkdir(parents=True)
    # Keep original identities/labels/history out of smoke input and provider payload.
    source={'channel':normalized.channel,'subject':normalized.subject,'body':normalized.body}
    input_path=output/'input.json';input_path.write_text(json.dumps([source],indent=2))
    os.environ.setdefault('HF_HOME',str(ROOT/'storage/cache/huggingface'))
    settings=Settings(storage=output,model=args.model,decider_mode=args.decider_mode)
    # Bounded one-attempt development probe; do not mistake this for quota preflight.
    settings.max_retries=0
    runtime=create_runtime(settings);settings,store,pipeline=runtime
    manifest={'kind':'single_live_development_full_workflow','calibration_index':args.index,
              'source_data_hash':split['data_hash'],'no_customer_delivery':True,
              'model':pipeline.provider.model,'decider_mode':pipeline.decider_mode,
              'fixture_or_replay':False,'main_operator_settings_changed':False}
    try:
        preflight=pipeline.provider.preflight()
        manifest['free_provider_verified']=preflight.get('verified') is True
        if not manifest['free_provider_verified']:raise RuntimeError('No verified free generation endpoint')
        if pipeline.evaluator:pipeline.evaluator.provider.preflight()
        store.set_control(True,'Explicit isolated live development answer smoke','implementation_coordinator')
        result,metrics=run(input_path,output/'results',runtime=runtime,kind='development')
        manifest['run_state']=result['state'];manifest['counts']=metrics['counts']
    finally:
        store.set_control(False,'Isolated development answer smoke ended','implementation_coordinator')
        (output/'smoke-manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps(manifest))

if __name__=='__main__':main()
