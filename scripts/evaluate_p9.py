"""Run a frozen P9 evaluation via the production HTTP application."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evaluation.runner import main
if __name__=='__main__':main()
