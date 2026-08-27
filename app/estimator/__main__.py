# app/estimator/__main__.py — enables `python -m app.estimator`
import sys
from .cli import main

if __name__ == "__main__":
    sys.exit(main())
