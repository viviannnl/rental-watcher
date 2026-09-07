import os
import sys
import tempfile

# The real database is a working record of every place found and emailed, and db.py
# connects and migrates at import time. Point anything that imports it at a throwaway
# file before that can happen.
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="rental-tests-"), "test.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
