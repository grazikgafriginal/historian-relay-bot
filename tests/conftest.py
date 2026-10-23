import sys
from pathlib import Path

# Make sure `import historian_relay_bot` works regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
