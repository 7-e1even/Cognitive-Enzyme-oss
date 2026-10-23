"""Small, explicit judge controls; never included in benchmark scores."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from types import SimpleNamespace

from tests.eval.hardbench.run import JUDGE_PROMPT, parse_verdict
from tests.eval.hardbench.rsi import OUT
from tests.eval.realbench.uplift_eval import ask_with_retry


CASES = [
    ('equal_fraction', 'Give one half as a number.', '1/2', '0.5', True),
    ('different_value', 'Compute 1+1.', '2', '3', False),
    ('unasked_reference_parts', '(1) Compute 1+1.', '(1) 2; (2) 3; (3) 4', '(1) 2', True),
    ('missing_requested_part', '(1) Compute 1+1. (2) Compute 1+2.', '(1) 2; (2) 3', '(1) 2', False),
    ('unit_conversion', 'Give the wavelength.', '720 nm', '7.2e-7 m', True),
    ('different_wavelength', 'Give the wavelength.', '720 nm', '500 nm', False),
]


async def main() -> None:
    args = SimpleNamespace(runner='opencode', model='opencode/deepseek-v4.1-flash-free', thinking='low',
        max_output_tokens=4096, timeout=60, max_wait=0, purpose='grade', service_retries=0, opencode_bin='')
    results = []
    for name, problem, reference, candidate, expected in CASES:
        prompt = JUDGE_PROMPT.format(domain='physics', problem=problem, answer=reference, pred=candidate)
        response = await ask_with_retry(prompt, args, dict(os.environ), OUT, lambda msg: print(msg, flush=True))
        actual = parse_verdict(response.get('text') or '') if response['ok'] else None
        result = {'id': name, 'expected': expected, 'actual': actual, 'passed': actual is expected,
                  'problem': problem, 'reference': reference, 'candidate': candidate, 'response': response}
        results.append(result)
        print(f'{name}: expected={expected}, actual={actual}, passed={result["passed"]}', flush=True)
    data = {'model': args.model, 'thinking': args.thinking,
            'judge_prompt_sha256': hashlib.sha256(JUDGE_PROMPT.encode()).hexdigest(), 'results': results}
    path = OUT / 'judge_calibration.json'
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    if not all(r['passed'] for r in results):
        raise SystemExit('Judge calibration incomplete or failed; inspect judge_calibration.json')


if __name__ == '__main__':
    asyncio.run(main())
