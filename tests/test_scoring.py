import sys; sys.path.insert(0,'.')
from app.matching.scorer import compute_score, _recency_score
from app.core import Enzyme, Pattern
from datetime import datetime, timezone, timedelta

print('=== Recency Decay ===')
e = Enzyme(patterns=[Pattern(value='t')], then='t')
print(f'Never triggered: {_recency_score(e):.3f}')
for d in [0, 7, 30, 90, 180]:
    e.last_triggered = datetime.now(timezone.utc) - timedelta(days=d)
    print(f'{d:3d} days ago:     {_recency_score(e):.3f}')

print()
print('=== Soft Degradation Simulation ===')
e = Enzyme(patterns=[Pattern(value='t')], then='t', confidence=0.5)
for i in range(5):
    old = e.confidence
    e.confidence *= 0.5
    action = 'degraded' if e.confidence > 0.1 else 'quarantine'
    print(f'Round {i+1}: {old:.3f} -> {e.confidence:.3f} [{action}]')

print()
print('All OK')
