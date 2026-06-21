"""
app.py — App factory. Wires blueprints together and initializes the DB.

This is the ONLY file that should change when you add a brand-new module
(a new blueprint file). It should never grow business logic itself.
"""

# MUST run before any other project imports -- core/category_engine.py reads
# GROQ_API_KEY from os.environ at import time (module-level), not lazily, so
# .env has to be loaded into the environment before that import happens.
from dotenv import load_dotenv
load_dotenv()

from flask import Flask
from detector import registry
from core.db import init_db
from core.category_master import seed_categories
from routes.parser_routes import parser_bp
from routes.workflow_routes import workflow_bp
import os


def create_app():
    app = Flask(__name__, static_folder=".")
    registry.auto_register(os.path.join(os.path.dirname(__file__), "parsers"))

    init_db()
    seed_categories()

    app.register_blueprint(parser_bp)
    app.register_blueprint(workflow_bp)
    return app


if __name__ == "__main__":
    app = create_app()
    print("\n🟢  DocParse v4 (modular)  →  http://localhost:5050\n")
    print("   Parsers:")
    for p in registry.list_parsers():
        print(f"     {p['bank_id']:12s} {p['display_name']}")
    print()
    app.run(port=5050, debug=False)
