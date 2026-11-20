"""Ordinary development input through shared live pipeline, separate from fixtures."""
import argparse,json
from pathlib import Path
from evaluation.harness import run

def main():
    p=argparse.ArgumentParser();p.add_argument('--input',default='tests/fixtures/four_channels.json');p.add_argument('--output',required=True);args=p.parse_args()
    from src.runtime import create_runtime
    runtime=create_runtime();settings,store,pipeline=runtime
    if not settings.api_key:raise SystemExit('Set OPENROUTER_API_KEY locally before live smoke; no fixture substitution.')
    pipeline.provider.preflight()
    print(run(args.input,args.output,runtime=runtime)[0]['state'])
if __name__=='__main__':main()
