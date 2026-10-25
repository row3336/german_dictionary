"""Build the immutable data18 v2 draft. No database is opened."""
import sys
from scripts.dictionary_import_v2 import main
if __name__ == "__main__":
    raise SystemExit(main(["build", *sys.argv[1:]]))
